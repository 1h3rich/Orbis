/* Orbis Estudio LWC v5: gráfico embebido con Lightweight Charts v5.0.9 real
 * vendorizado local (sin CDN/npm).
 * Usa window.LightweightCharts (v5.0.9 real, Apache-2.0): createChart,
 * addSeries(CandlestickSeries/LineSeries/AreaSeries/BarSeries/HistogramSeries),
 * createSeriesMarkers, panes addSeries(...,paneIndex) + panes()[i].setHeight,
 * UTCTimestamp en segundos, update vs setData con guardado de rango visible,
 * coordinateToPrice/priceToCoordinate. Si el vendor falta: mensaje honesto.
 * Dibujos como IPanePrimitive (ver DrawingPrimitive): H, tendencia 2pts,
 * vertical, rectángulo zona, texto, medidor, long/short con riesgo
 * matemático, fib retracement + extension 3pts, trazo libre (mano alzada).
 * Fase 3: todo dibujo no bloqueado se mueve arrastrando su cuerpo y cambia
 * de forma arrastrando sus puntos de control (hitTest.part); grosor, estilo
 * discontinuo, opacidad y duplicado en el panel flotante. Persistencia por
 * mercado+provider(+timeframe) en /ui/state + localStorage.
 * Barra de dibujo dockable TradingView (izquierda/derecha/arriba/abajo) con
 * una única preferencia compacta dentro de Estudio.
 * Navegación (Fase 2, decisión documentada): delegada al motor real.
 * createChart nace con handleScroll {mouseWheel, pressedMouseMove} y
 * handleScale {mouseWheel, pinch, axisPressedMouseMove,
 * axisDoubleClickReset} activados: la rueda acerca centrada en el puntero
 * (el motor ancla el zoom en clientX: deriva 0 px medida en harness), la
 * rueda con Mayús (deltaX) desplaza, el arrastre mueve y el eje de precios
 * se arrastra para fijar la escala. Se eliminó el zoom/pan propios
 * (onChartWheel/onPanMouse*): duplicaban al motor con peor anclaje.
 * Con herramienta de dibujo activa se desactivan (handleScroll/Scale en
 * false) y al salir se restauran las nativas. Escala de precios: si el
 * usuario la fija (arrastre en el eje), se marca priceUserFixed y el motor
 * la conserva en pan/zoom/update (verificado en harness); Centrar o doble
 * clic en el eje la devuelven a automática. Si el paneo sale del
 * histórico sin velas visibles, se congela el último rango con datos
 * (guardPriceOnScroll) en vez de colapsar la escala.
 * la convención SVG (tx/tAt) usa la misma ventana vía timeToCoordinate /
 * coordinateToTime (con fallback lineal honesto si devuelven null).
 * Funciones del Estudio: submenú de apertura única (un panel visible).
 * Pantalla completa sobre .studio con :fullscreen + fallback
 * .studio-expanded; ResizeObserver/fullscreenchange recalculan el tamaño.
 * Refresco Auto por defecto según temporalidad (ver autoRefreshFor); el
 * temporizador se reinicia al cambiar de TF. Timers independientes:
 * Estudio auto, Operativa 15 s y Portfolio 24 h (app.js).
 */
(function () {
  "use strict";

  var TF_LIST = ["1s", "30s", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"];
  var PROVIDERS = ["auto", "kraken", "binance", "coinbase", "okx", "bybit", "mexc", "kucoin", "gate", "bitget", "grvt", "bitstamp"];
  var CHART_TYPES = [["velas", "Velas"], ["heikin", "Heikin-Ashi"], ["linea", "Línea"], ["area", "Área"], ["barras", "Barras"]];
  var FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  var FIB_EXT = [0.618, 1, 1.272, 1.618, 2, 2.618];
  var FIB_DEFAULT = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  var FIB_SUGGEST = [1.272, 1.618, 2.618];
  var LAYOUTS_KEY = "orbis.estudio.layouts";
  var DOCK_OPTS = ["izquierda", "derecha", "arriba", "abajo"];
  var REFRESH_MANUAL_VALUES = [0, 1000, 5000, 15000, 60000, 300000];
  /* Duración de cada vela en segundos (Fase 4: ÚNICA fuente del refresco
   * Auto). 1s existe en binance/okx/gate y 30s solo en okx (verificado
   * contra las APIs; el backend responde available:false honesto cuando un
   * TF no es nativo); 1M (mensual) en
   * binance/okx/bybit/mexc/kucoin/bitget (interval=1M, bar=1M, interval=M,
   * granularity=1M, type=1month; kraken/coinbase/gate/grvt no lo sirven);
   * el resto de valores coincide con lo que sirve cada exchange. 1M usa
   * mayúscula a propósito: "1m" ya es el minuto y el regex de tfSeconds no
   * la toca. 1M aquí es una aproximación sólo para cadencia y distancia de
   * marcadores; el rolado de velas usa meses UTC de calendario. */
  var TF_SECONDS = { "1s": 1, "30s": 30, "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800, "1M": 2592000 };
  function tfSeconds(tf) {
    if (TF_SECONDS[tf]) return TF_SECONDS[tf];
    var m = /^(\d+)(s|m|h|d|w)$/.exec(String(tf || ""));
    if (!m) return 0;
    return Number(m[1]) * ({ s: 1, m: 60, h: 3600, d: 86400, w: 604800 }[m[2]]);
  }
  function candleEndTime(start, seconds, monthly) {
    if (!monthly) return start + seconds;
    var date = new Date(start * 1000);
    return Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 1) / 1000;
  }
  /* Refresco Auto = cuarta parte de la duración de la vela, en segundos
   * enteros, entre 1 s y 60 s. Una sola regla (antes había dos verdades:
   * tabla fija + fórmula tf/2 con topes). Equivale a: 1s→1s, 30s→8s
   * (7,5 s redondeado), 1m→15s, 5m y mayores→60s. Nunca más de 1 pet/s
   * por ventana. Estudio usa este auto; Operativa va a 15 s y Portfolio a
   * 24 h en app.js (timers independientes, nada de temporizador global). */
  function autoRefreshFor(tf) {
    var s = tfSeconds(tf);
    if (!(s > 0)) return 30000;
    var ms = Math.round(s * 1000 / 4 / 1000) * 1000;
    if (ms < 1000) ms = 1000;
    if (ms > 60000) ms = 60000;
    return ms;
  }
  function fmtRefreshMs(ms) {
    ms = Number(ms);
    if (ms >= 60000) return (ms / 60000) + " min";
    return (ms / 1000) + " s";
  }
  /* M5: disponibilidad de TF derivada del backend, sin tabla hardcodeada.
   * No hay lista a priori de "qué TF sirve cada proveedor": cada
   * available:false observado en la sesión cachea el par proveedor/TF con
   * su reason EXACTO (markTfUnavailable) y el selector deshabilita esa
   * opción con ese texto (updateTfAvailability, dentro de render). Lo no
   * probado aparece habilitado hasta que falla —y el fallo ya es honesto
   * (loadCandles muestra el reason)—, y un éxito posterior rehabilita el
   * par (markTfAvailable). La clave es el resolved_provider del backend
   * (con el proveedor pedido como reserva); con "auto" no se puede saber
   * qué respaldo resolverá, así que nada se deshabilita por adelantado. */
  var TF_OBSERVED_UNAVAILABLE = {};
  function tfUnavailableReason(prov, tf) {
    var per = TF_OBSERVED_UNAVAILABLE[String(prov || "")];
    if (!per) return "";
    var r = per[String(tf || "")];
    return typeof r === "string" ? r : "";
  }
  function markTfUnavailable(prov, tf, reason) {
    prov = String(prov || "");
    tf = String(tf || "");
    if (!prov || !tf) return;
    if (!TF_OBSERVED_UNAVAILABLE[prov]) TF_OBSERVED_UNAVAILABLE[prov] = {};
    TF_OBSERVED_UNAVAILABLE[prov][tf] = String(reason || "no disponible");
  }
  function markTfAvailable(prov, tf) {
    prov = String(prov || "");
    tf = String(tf || "");
    var per = TF_OBSERVED_UNAVAILABLE[prov];
    if (per && Object.prototype.hasOwnProperty.call(per, tf)) delete per[tf];
  }

  /* Active cleanup wrapper: set by render(), called by exported cleanup. */
  var activeCleanup = null;

  function num(v, d) { var n = Number(v); return isFinite(n) ? n : (d === undefined ? 0 : d); }
  function canon(sym) { return String(sym || "BTC/EUR").trim().toUpperCase().replace("-", "/").replace("_", "/").replace(" ", ""); }
  function isValidHexColor(v) {
    var s = String(v || "").trim();
    return /^#[0-9a-fA-F]{6}$/.test(s);
  }
  function canon2(sym) {
    var s = canon(sym);
    if (s.indexOf("/") < 0) {
      for (var k = 4; k >= 3; k--) {
        if (s.length > k) return s.slice(0, -k) + "/" + s.slice(-k);
      }
    }
    return s;
  }
  function precision(symbol) {
    var base = canon2(symbol).split("/")[0];
    if (base === "BTC" || base === "ETH") return 2;
    return 8; /* activos de precio bajo: 8 decimales */
  }
  function fmtP(v, symbol) {
    var n = Number(v);
    if (!isFinite(n)) return "—";
    return n.toLocaleString("es-ES", { minimumFractionDigits: precision(symbol), maximumFractionDigits: precision(symbol) });
  }

  /* ---------------- IPanePrimitive: dibujos ----------------
   * Cada dibujo es un IPanePrimitive lógico: {id, type, pts:[{t,p}],
   * opts, locked, hidden} con draw(svg, conv), hitTest(x,y,conv) y
   * move/extremos. Se pinta en una capa SVG sobre el pane de precio,
   * usando coordinateToPrice/priceToCoordinate + timeToCoordinate. */
  function DrawingPrimitive(obj) {
    this.o = obj;
  }
  DrawingPrimitive.prototype.hitTest = function (x, y, conv) {
    /* Tolerancia con grosor (Fase 3): un trazo grueso no se vuelve
     * imposible de seleccionar; uno fino conserva el tacto anterior
     * (6 px cuerpo / 10 px extremos con width 1.5). */
    var wTol = effWidth(this.o, 1.5);
    var tol = 6 + wTol / 2;
    var endR2 = Math.pow(10 + wTol / 2, 2);
    var isFH = this.o.type === "freehand";
    var pts = (this.o.pts || []).map(function (q) { return { x: conv.tx(q.t), y: conv.py(q.p) }; });
    for (var i = 0; i < pts.length; i++) {
      /* En trazo libre solo los extremos se agarran; el resto del trazo
       * selecciona el cuerpo (si no, cada vértice intermedio sería un
       * punto de control y deformaría el gesto al moverlo). */
      if (isFH && i !== 0 && i !== pts.length - 1) continue;
      var dx = pts[i].x - x, dy = pts[i].y - y;
      if (dx * dx + dy * dy < endR2) return { part: i };
    }
    /* línea horizontal/vertical: probar distancia a la línea */
    if (this.o.type === "h" && this.o.pts[0]) {
      var y2 = conv.py(this.o.pts[0].p);
      if (Math.abs(y2 - y) < tol) return { part: -1 };
    }
    if (this.o.type === "v" && this.o.pts[0]) {
      var x2 = conv.tx(this.o.pts[0].t);
      if (Math.abs(x2 - x) < tol) return { part: -1 };
    }
    if (this.o.type === "alertline" && this.o.pts[0]) {
      var y2 = conv.py(this.o.pts[0].p);
      if (Math.abs(y2 - y) < tol) return { part: -1 };
    }
    if ((this.o.type === "trend" || this.o.type === "measure") && this.o.pts.length >= 2) {
      var a = { x: conv.tx(this.o.pts[0].t), y: conv.py(this.o.pts[0].p) };
      var b = { x: conv.tx(this.o.pts[1].t), y: conv.py(this.o.pts[1].p) };
      if (distToSegment(x, y, a, b) < tol) return { part: -1 };
    }
    if ((this.o.type === "fib" || this.o.type === "fibext") && this.o.pts.length >= 2) {
      var a = { x: conv.tx(this.o.pts[0].t), y: conv.py(this.o.pts[0].p) };
      var b = { x: conv.tx(this.o.pts[1].t), y: conv.py(this.o.pts[1].p) };
      if (distToSegment(x, y, a, b) < tol) return { part: -1 };
      /* también probar líneas de nivel */
      var hi = Math.max(num(this.o.pts[0].p), num(this.o.pts[1].p)), lo = Math.min(num(this.o.pts[0].p), num(this.o.pts[1].p));
      var dir = num(this.o.pts[1].p) >= num(this.o.pts[0].p) ? 1 : -1;
      var lvls = Array.isArray(this.o.levels) && this.o.levels.length ? this.o.levels : fibLevels;
      for (var li = 0; li < lvls.length; li++) {
        var pr = dir > 0 ? hi - (hi - lo) * num(lvls[li]) : lo + (hi - lo) * num(lvls[li]);
        var yy = conv.py(pr);
        if (Math.abs(yy - y) < tol && x >= Math.min(a.x, b.x) - 10 && x <= Math.max(a.x, b.x) + 120) return { part: -1 };
      }
    }
    if ((this.o.type === "long" || this.o.type === "short") && this.o.pts.length >= 2) {
      var a = { x: conv.tx(this.o.pts[0].t), y: conv.py(this.o.pts[0].p) };
      var b = { x: conv.tx(this.o.pts[1].t), y: conv.py(this.o.pts[1].p) };
      if (distToSegment(x, y, a, b) < tol) return { part: -1 };
      for (var pi = 0; pi < 3; pi++) {
        var pr = num(this.o.pts[pi] ? this.o.pts[pi].p : this.o.pts[0].p);
        var yy = conv.py(pr);
        if (Math.abs(yy - y) < tol && x >= Math.min(a.x, b.x) - 10 && x <= Math.max(a.x, b.x) + 120) return { part: -1 };
      }
    }
    if (this.o.type === "rect" && this.o.pts.length >= 2) {
      /* Zona con el mismo criterio que el resto (tol = 6 + grosor/2): con
       * grosor 4 la zona se agarra a 8 px del borde; con grosor 1, a 6,5 px.
       * El ±4 fijo anterior la hacía difícil de seleccionar en grueso y
       * demasiado fácil en fino. */
      var a = { x: conv.tx(this.o.pts[0].t), y: conv.py(this.o.pts[0].p) };
      var b = { x: conv.tx(this.o.pts[1].t), y: conv.py(this.o.pts[1].p) };
      if (x >= Math.min(a.x, b.x) - tol && x <= Math.max(a.x, b.x) + tol && y >= Math.min(a.y, b.y) - tol && y <= Math.max(a.y, b.y) + tol) return { part: -1 };
    }
    if (this.o.type === "text" && this.o.pts[0]) {
      /* Radio fijo de 12 px a propósito (fijado por test): el texto no es un
       * trazo y no tiene grosor de línea, así que no usa tol sensible al
       * grosor como el resto; el punto de agarre es siempre el mismo. */
      var isFloat = this.o.pinned === false && typeof this.o.fx === "number" && typeof this.o.fy === "number";
      var W = chartBox.clientWidth || 600;
      var H = chartBox.clientHeight || 380;
      var px = isFloat ? this.o.fx * W : conv.tx(this.o.pts[0].t);
      var py = isFloat ? this.o.fy * H : conv.py(this.o.pts[0].p);
      var dx = px - x, dy = py - y;
      if (dx * dx + dy * dy < 144) return { part: 0 };
    }
    /* Trazo libre (Fase 3): distancia al segmento más cercano de la
     * polilínea; los extremos ya se probaron arriba (part 0 / último). */
    if (this.o.type === "freehand" && this.o.pts.length >= 2) {
      for (var sg = 0; sg < pts.length - 1; sg++) {
        if (distToSegment(x, y, pts[sg], pts[sg + 1]) < tol) return { part: -1 };
      }
    }
    return null;
  };

  function distToSegment(px, py, a, b) {
    var dx = b.x - a.x, dy = b.y - a.y;
    var len2 = dx * dx + dy * dy;
    if (len2 === 0) return Math.hypot(px - a.x, py - a.y);
    var t = Math.max(0, Math.min(1, ((px - a.x) * dx + (py - a.y) * dy) / len2));
    var projX = a.x + t * dx, projY = a.y + t * dy;
    return Math.hypot(px - projX, py - projY);
  }

    /* ---------------- Fase 3: dibujos editables + mano alzada ----------------
     * Propiedades nuevas, retrocompatibles (ausentes = aspecto de siempre,
     * sin migración de datos): width (grosor en px SVG, defecto 1.5),
     * style ("solid"|"dashed"; por defecto cada herramienta conserva su
     * trazo propio) y opacity (0..1, defecto 1). Ayudantes puros (sin DOM
     * ni chart) para probarlos en Node con el harness existente. */
    var DRAW_WIDTHS = [1, 1.5, 2.5, 4];
    var FREEHAND_MIN_PX = 5;
    function effWidth(d, defW) {
      var w = num(d && d.width);
      if (!isFinite(w) || w < 0.5 || w > 8) return defW;
      return w;
    }
    function effDash(d, defDash) {
      if (d && d.style === "solid") return "";
      if (d && d.style === "dashed") return (typeof d.dash === "string" && d.dash) ? d.dash : "5 4";
      if (d && typeof d.dash === "string" && d.dash) return d.dash;
      return defDash || "";
    }
    function effOpacity(d) {
      var o = Number(d && d.opacity);
      if (!isFinite(o) || o < 0 || o > 1) return 1;
      return o;
    }
    function canDragDrawing(d) { return !!d && !d.locked; }
    /* Mueve el objeto completo: desplaza TODOS los pts (array nuevo, sin
     * mutar); el anclaje tiempo/precio hace que siga en las mismas
     * velas/precios tras zoom/pan. */
    function movePts(pts, dt, dp) {
      return (pts || []).map(function (q) { return { t: num(q.t) + num(dt), p: num(q.p) + num(dp) }; });
    }
    /* Mueve UN solo extremo (array nuevo; el resto queda intacto). */
    function moveEndpoint(pts, part, t, p) {
      return (pts || []).map(function (q, i) { return i === part ? { t: t, p: p } : { t: q.t, p: q.p }; });
    }
    function removeDrawing(list, id) {
      return (list || []).filter(function (o) { return o.id !== id; });
    }
    /* Duplicado con desplazamiento (dt en tiempo, dp en precio) e id nuevo;
     * copia profunda: tocar la copia no afecta al original. */
    function dupDrawing(d, dt, dp, newId) {
      var c = JSON.parse(JSON.stringify(d));
      c.id = newId;
      c.pts = movePts(c.pts || [], dt, dp);
      return c;
    }
    /* Cancela un arrastre de edición (Escape): restaura los puntos originales
     * guardados en ed.orig y retira el paso de undo que el gesto guardó al
     * empezar su primer movimiento real (push diferido en onChartMouseMove).
     * - Cuerpo (part -1): se restauran TODOS los puntos (ed.orig completo).
     * - Extremo (part >= 0): se restaura solo ed.orig[part]; el resto queda
     *   como está (equivale al original completo porque el gesto solo toca
     *   ese índice, pero así la intención queda explícita y probada).
     * - Texto flotante: no vive en pts sino en fx/fy (fracción de pantalla);
     *   se restauran fx0/fy0.
     * - Si el gesto aún no había movido nada (!ed.moved) el undo no se tocó:
     *   no hay pop. Si se movió, se retira el último paso y se devuelve el
     *   redo tal como estaba (redoBackup), para no dejar un Ctrl+Z fantasma
     *   ni borrar rehaceres ajenos al gesto cancelado.
     * Pura (sin DOM ni chart): se prueba en Node con el harness existente.
     * Devuelve true si había arrastre que cancelar. */
    function cancelEditDragApply(drawings, ed, undoStack, redoStack) {
      if (!ed) return false;
      var obj = null;
      for (var ci = 0; ci < (drawings || []).length; ci++) {
        if (drawings[ci] && drawings[ci].id === ed.id) { obj = drawings[ci]; break; }
      }
      if (obj) {
        if (obj.type === "text" && obj.pinned === false) {
          if (typeof ed.fx0 === "number" && isFinite(ed.fx0)) obj.fx = ed.fx0;
          if (typeof ed.fy0 === "number" && isFinite(ed.fy0)) obj.fy = ed.fy0;
        } else if (Array.isArray(ed.orig)) {
          if (ed.part >= 0 && ed.part < ed.orig.length && Array.isArray(obj.pts)) {
            var cp = ed.part, co = ed.orig;
            obj.pts = obj.pts.map(function (q, idx) {
              return idx === cp ? { t: co[idx].t, p: co[idx].p } : { t: q.t, p: q.p };
            });
          } else {
            obj.pts = JSON.parse(JSON.stringify(ed.orig));
          }
        }
      }
      if (ed.moved) {
        if (Array.isArray(undoStack) && undoStack.length) undoStack.pop();
        if (Array.isArray(ed.redoBackup) && Array.isArray(redoStack)) {
          redoStack.length = 0;
          for (var ri = 0; ri < ed.redoBackup.length; ri++) redoStack.push(ed.redoBackup[ri]);
        }
      }
      return true;
    }
    /* Trazo libre: píxeles del gesto → tiempo/precio con la misma conv
     * (svgConv); nunca se guardan píxeles, así que sobrevive a zoom/pan. */
    function strokeToTimePrice(px, conv) {
      return (px || []).map(function (q) { return { t: conv.tAt(q.x), p: conv.pAt(q.y) }; });
    }
    /* Muestreo del trazo: conserva primero y último, descarta los que caen
     * a menos de minDist px del último conservado (un gesto de ~300 eventos
     * queda en ~60-120 puntos: render fluido, persistencia sin inflar). */
    function decimateStroke(px, minDist) {
      var out = [], md = Math.max(1, num(minDist, 5));
      for (var di = 0; di < (px || []).length; di++) {
        var q = px[di];
        if (!out.length || di === px.length - 1 ||
            Math.hypot(q.x - out[out.length - 1].x, q.y - out[out.length - 1].y) >= md) {
          out.push({ x: q.x, y: q.y });
        }
      }
      return out;
    }

  /* ---------------- matemática de indicadores (presentación) ----------------
   * closes() acepta velas ya mapeadas ({close}) y filas crudas del API
   * ({c}): loadCandles mapea a {time,open,high,low,close,v} antes de
   * llamar a los indicadores, así que leer solo r.c devolvía ceros. */
  function closes(rows) { return rows.map(function (r) { return num(r.close !== undefined && r.close !== null ? r.close : r.c); }); }
  /* Fila cruda del API ({t,o,h,l,c,v}) a vela del Estudio
   * ({time,open,high,low,close,v}): un solo punto de mapeo usado por
   * loadCandles y por el refresco incremental (sin duplicar lógica). */
  function mapCandleRow(c) {
    return { time: num(c.t), open: c.o, high: c.h, low: c.l, close: c.c, v: c.v };
  }
  /* Funde filas mapeadas en el dataset (Fase 4, refresco de la vela
   * abierta): actualiza tiempos coincidentes y añade los nuevos ordenados.
   * Devuelve {grew} (true si llegó alguna vela nueva). Pura y testeable. */
  function mergeCandleRows(existing, rows) {
    var byT = {}, grew = false, i;
    for (i = 0; i < existing.length; i++) byT[existing[i].time] = i;
    for (i = 0; i < (rows || []).length; i++) {
      var r = rows[i];
      if (r === null || r === undefined || !isFinite(+r.time)) continue;
      if (byT[r.time] !== undefined) existing[byT[r.time]] = r;
      else { existing.push(r); grew = true; }
    }
    if (grew) existing.sort(function (a, b) { return a.time - b.time; });
    return { grew: grew };
  }
  /* Guarda WS↔REST (Fase 4b, pura y testeable): `ws.onmessage` muta la
   * última vela en local mientras `refreshOpenCandle` trae el REST pedido
   * antes; si un tick llegó durante el vuelo, la respuesta puede traer un
   * cierre más viejo y `mergeCandleRows` lo sobrescribiría (retroceso).
   * Con esta poda el refresco solo añade tiempos estrictamente nuevos y no
   * pisa lo que el tick ya avanzó. Sin tick en vuelo devuelve `rows` tal
   * cual (sin cambios de comportamiento). */
  function pruneRefreshRows(lastTime, rows, wsMoved) {
    if (!wsMoved) return rows;
    return (rows || []).filter(function (r) {
      return r !== null && r !== undefined && isFinite(+r.time) && (+r.time > +lastTime);
    });
  }
  function nextCandleStart(lastTime, tfSecs, tf) {
    var t = Number(lastTime), s = Number(tfSecs);
    if (!isFinite(t) || t < 0 || !isFinite(s) || !(s > 0)) return null;
    if (tf === "1M") {
      var date = new Date(t * 1000);
      var next = Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 1) / 1000;
      return isFinite(next) ? next : null;
    }
    if (tf === "1w") return t + s; /* conserva el ancla semanal del venue */
    return (Math.floor(t / s) + 1) * s;
  }
  /* Rola sólo al comienzo real de la siguiente vela: mes UTC para 1M y
   * semanas ancladas a la apertura que entregó el proveedor para 1w. Los
   * demás TF usan su rejilla fija. Un hueco abre sólo la vela del tick, sin
   * inventar barras intermedias; un tick anterior nunca reescribe el cierre. */
  function rollCandleIfNeeded(candles, tick, tfSecs, tf) {
    var last = (candles && candles.length) ? candles[candles.length - 1] : null;
    var t = Math.floor(Number(tick && tick.t));
    var px = tick ? tick.price : null;
    var s = Number(tfSecs);
    function mutateLast() {
      if (!last || px === null || px === undefined || px === "" ||
          !isFinite(Number(px)) || !(Number(px) > 0)) return false;
      last.close = String(px);
      last.high = String(Math.max(num(last.high), num(px)));
      last.low = String(Math.min(num(last.low), num(px)));
      return false;
    }
    if (!last || !isFinite(t) || !(s > 0)) return mutateLast();
    var lastTime = Number(last.time);
    if (!isFinite(lastTime) || lastTime < 0 || t < lastTime ||
        px === null || px === undefined || px === "" ||
        !isFinite(Number(px)) || !(Number(px) > 0)) return false;
    var nextStart = nextCandleStart(lastTime, s, tf), start;
    if (nextStart === null) return false;
    if (tf === "1M") {
      var tickDate = new Date(t * 1000);
      start = Date.UTC(tickDate.getUTCFullYear(), tickDate.getUTCMonth(), 1) / 1000;
    } else if (tf === "1w") {
      start = lastTime + Math.floor((t - lastTime) / s) * s;
    } else {
      start = Math.floor(t / s) * s;
    }
    if (t >= nextStart) {
      candles.push({ time: start, open: px, high: px, low: px, close: px, v: 0 });
      return true;
    }
    return mutateLast();
  }
  /* Clave de la caché de indicadores (Fase 4, extraída para testear): la
   * forman longitud, primera/última vela, último cierre, mercado,
   * temporalidad y parámetros. Cambiar de TF o de proveedor invalida
   * siempre (no quedan datos de la TF anterior); repetir con lo mismo
   * reutiliza los arrays calculados. */
  function indCacheKey(n, firstT, lastT, lastClose, market, timeframe, paramsJson) {
    return [n, firstT, lastT, String(lastClose), market, timeframe, paramsJson].join("|");
  }
  /* Configuración de indicadores saneada (reparación Fase 5, pura y
   * testeable): el local o el backend pueden traer basura (null, claves
   * ausentes, periodos rotos) y paintInds/paintIndicators leen
   * indCfg[key].on y los parámetros sin comprobar. Se fusiona con los
   * defectos (todo apagado); ema.p conserva la lista "9,20,50" tal cual
   * si trae algo válido (parsePeriodList decide) para no romper la
   * persistencia; el resto son numéricos >= 1 o el defecto. */
  var DEFAULT_IND = { sma: { on: false, p: 20 }, ema: { on: false, p: 20 }, bb: { on: false, p: 20, m: 2 }, rsi: { on: false, p: 14 }, macd: { on: false, f: 12, s: 26, sig: 9 } };
  /* Decide si un payload de /ux/state kind=indicators debe aplicarse
   * (R1, puro y testeable): el backend devuelve ``payload: {}`` cuando NO
   * hay fila guardada para ese mercado+proveedor+TF (ver ui_state_get en
   * app/web/mount.py: ``row.payload if row else {}``), y ``{}`` pasaba el
   * guard anterior (truthy, typeof "object") hasta sanitizeIndCfg, que lo
   * convertía en todo-apagado y BORRABA los indicadores activos al cambiar
   * a cualquier mercado sin fila guardada. Vacío (null, no-objeto o sin
   * claves) = "el backend no tiene nada": NO se aplica y se conserva lo que
   * haya, como documenta loadPersisted. Una fila guardada de verdad siempre
   * trae claves (persistInd envía el objeto completo de 5 indicadores,
   * aunque estén todos apagados), así que exigir claves no confunde un
   * "todo apagado" explícito con un "sin nada". */
  function shouldApplyIndPayload(pl) {
    if (!pl || typeof pl !== "object" || Array.isArray(pl)) return false;
    return Object.keys(pl).length > 0;
  }
  function sanitizeIndCfg(v) {
    var o = (v && typeof v === "object" && !Array.isArray(v)) ? v : {};
    function numP(x, d) { var n = Number(x); return (isFinite(n) && n >= 1) ? n : d; }
    function keepEmaP(x, d) {
      if (typeof x === "string") return x.trim() !== "" ? x : d;
      if (Array.isArray(x)) return x.length ? x : d;
      return (isFinite(Number(x)) && Number(x) >= 1) ? Number(x) : d;
    }
    function sub(key, fb) {
      var s = (o[key] && typeof o[key] === "object" && !Array.isArray(o[key])) ? o[key] : {};
      var r = { on: s.on === true };
      Object.keys(fb).forEach(function (k) {
        if (k === "on") return;
        r[k] = (key === "ema" && k === "p") ? keepEmaP(s[k], fb[k]) : numP(s[k], fb[k]);
      });
      return r;
    }
    return { sma: sub("sma", DEFAULT_IND.sma), ema: sub("ema", DEFAULT_IND.ema), bb: sub("bb", DEFAULT_IND.bb), rsi: sub("rsi", DEFAULT_IND.rsi), macd: sub("macd", DEFAULT_IND.macd) };
  }
  function sma(vals, p) {
    var out = [], acc = 0;
    for (var i = 0; i < vals.length; i++) {
      acc += vals[i];
      if (i >= p) acc -= vals[i - p];
      out.push(i >= p - 1 ? acc / p : null);
    }
    return out;
  }
  /* EMA con semilla SMA (Fase 1): EMA_t = P_t·k + EMA_(t-1)·(1−k) con
   * k = 2/(p+1), inicializada como la SMA de los primeros p precios. El
   * primer valor válido está en el índice p−1 y es exactamente esa SMA;
   * antes, null. Sembrar con vals[0] (código anterior) sesgaba toda la
   * curva; la orientación de los datos no cambia (cronológico ascendente). */
  function ema(vals, p) {
    p = Math.max(1, Math.floor(num(p, 1)));
    var out = [], k = 2 / (p + 1), e = null, seed = null;
    if (vals.length >= p) {
      var acc = 0;
      for (var s = 0; s < p; s++) acc += vals[s];
      seed = acc / p;
    }
    for (var i = 0; i < vals.length; i++) {
      if (i < p - 1) { out.push(null); continue; }
      e = (i === p - 1) ? seed : vals[i] * k + e * (1 - k);
      out.push(e);
    }
    return out;
  }
  function bollinger(vals, p, mult) {
    var mid = sma(vals, p), up = [], lo = [];
    for (var i = 0; i < vals.length; i++) {
      if (i < p - 1) { up.push(null); lo.push(null); continue; }
      var m = mid[i], v = 0;
      for (var j = i - p + 1; j <= i; j++) v += (vals[j] - m) * (vals[j] - m);
      var sd = Math.sqrt(v / p);
      up.push(m + mult * sd); lo.push(m - mult * sd);
    }
    return { mid: mid, up: up, lo: lo };
  }
  function rsi(vals, p) {
    var out = [], g = 0, l = 0;
    for (var i = 1; i < vals.length; i++) {
      var d = vals[i] - vals[i - 1];
      g = (g * (p - 1) + Math.max(0, d)) / p;
      l = (l * (p - 1) + Math.max(0, -d)) / p;
      out.push(i >= p ? (l === 0 ? 100 : 100 - 100 / (1 + g / l)) : null);
    }
    out.unshift(null);
    return out;
  }
  function macd(vals, f, s, sig) {
    var ef = ema(vals, f), es = ema(vals, s), line = [], sg = [];
    for (var i = 0; i < vals.length; i++) {
      line.push((ef[i] === null || es[i] === null) ? null : ef[i] - es[i]);
    }
    var first = line.findIndex(function (v) { return v !== null; });
    var se = first < 0 ? [] : ema(line.slice(first), sig);
    for (var j = 0; j < vals.length; j++) sg.push(first < 0 || j < first ? null : se[j - first]);
    return { line: line, signal: sg };
  }

  /* ---------------- periodos múltiples + historial suficiente (Fase 1) ----
   * indCfg.ema.p admite un número (legado, equivale a un solo periodo) o
   * una lista "9,20,50" (comas, puntos y coma o espacios). Devuelve
   * periodos enteros >= 1, únicos y ordenados; si no hay ninguno válido,
   * el fallback fb. La persistencia no cambia (se guarda el valor tal cual).
   * Colores estables por periodo: los habituales tienen color fijo (el 20
   * conserva el morado histórico) y el resto cae por hash en la paleta. */
  var EMA_PALETTE = ["#7c5cff", "#4da3ff", "#e3b341", "#3fb950", "#f85149", "#d29922", "#ab7df8", "#39c5cf"];
  var EMA_FIXED_COLORS = { 9: "#4da3ff", 20: "#7c5cff", 50: "#e3b341", 100: "#3fb950", 200: "#f85149" };
  function parsePeriodList(v, fb) {
    var out = [];
    var parts = Array.isArray(v) ? v : String(v === undefined || v === null ? "" : v).split(/[,;\s]+/);
    for (var i = 0; i < parts.length; i++) {
      var p = Math.floor(Number(String(parts[i]).replace(",", ".")));
      if (isFinite(p) && p >= 1 && out.indexOf(p) < 0) out.push(p);
    }
    out.sort(function (a, b) { return a - b; });
    if (!out.length && Array.isArray(fb) && fb.length) return fb.slice();
    return out;
  }
  function emaColorFor(p) {
    if (EMA_FIXED_COLORS[p]) return EMA_FIXED_COLORS[p];
    return EMA_PALETTE[p % EMA_PALETTE.length];
  }
  /* Periodo máximo entre los indicadores ACTIVOS (on). Sin indicadores de
   * ventana larga devuelve 0. */
  function maxWindowPeriod(indCfg) {
    var mx = 0;
    try {
      if (indCfg.sma && indCfg.sma.on) mx = Math.max(mx, num(indCfg.sma.p, 0));
      if (indCfg.ema && indCfg.ema.on) {
        parsePeriodList(indCfg.ema.p, []).forEach(function (p) { mx = Math.max(mx, p); });
      }
      if (indCfg.bb && indCfg.bb.on) mx = Math.max(mx, num(indCfg.bb.p, 0));
      if (indCfg.rsi && indCfg.rsi.on) mx = Math.max(mx, num(indCfg.rsi.p, 0));
      if (indCfg.macd && indCfg.macd.on) {
        mx = Math.max(mx, num(indCfg.macd.f, 0), num(indCfg.macd.s, 0), num(indCfg.macd.sig, 0));
      }
    } catch (e) {}
    return mx;
  }
  /* Historial a pedir: limit = clamp(maxPeriodo × 4, 500, 1000). Con
   * EMA-200 activa pide 800 velas (converge de verdad); sin indicadores de
   * ventana larga, 500. Nunca pasa de 1000 (tope del endpoint /market/candles). */
  function candleLimitFor(indCfg) {
    var mx = maxWindowPeriod(indCfg);
    var lim = mx > 0 ? mx * 4 : 500;
    if (lim < 500) lim = 500;
    if (lim > 1000) lim = 1000;
    return lim;
  }

  function persistenceKey(market, provider, timeframe, kind) {
    return "orbis.estudio." + market + "|" + provider + "|" + timeframe + "|" + kind;
  }
  function loadLocal(market, provider, timeframe, kind, fb) {
    try {
      var raw = localStorage.getItem(persistenceKey(market, provider, timeframe, kind));
      if (raw) return JSON.parse(raw);
    } catch (e) {}
    return fb;
  }
  function saveLocal(market, provider, timeframe, kind, val) {
    try { localStorage.setItem(persistenceKey(market, provider, timeframe, kind), JSON.stringify(val)); } catch (e) {}
  }

  /* ---------------- CONSTANTES DE TIMERS POR VISTA ---------------- */
  var ESTUDIO_REFRESH_MS = 30000;  // 30s; Operativa=15s y Portfolio=24h viven en app.js

  function render(root, U, params) {
    /* New render cleans up previous instance if any. */
    if (activeCleanup) { var fn = activeCleanup; activeCleanup = null; fn(); }

    var market = canon2(loadLocal("cfg", "all", "x", "lastmarket", "BTC/EUR"));
    var requestedMarket = params && params.get("market");
    if (requestedMarket && /^[A-Z0-9]{2,12}\/(EUR|USD|USDT)$/.test(requestedMarket)) {
      market = requestedMarket;
      saveLocal("cfg", "all", "x", "lastmarket", market);
    }
    /* La fuente del Estudio es una preferencia local de esta herramienta:
     * consultar BTC 2012 en Bitstamp no debe dejar sin 24h a Operativa. El
     * selector general sólo da el valor inicial y sigue pudiendo cambiarla
     * explícitamente mediante el evento orbis:provider-change. */
    var provider = loadLocal("cfg", "all", "x", "provider", U.orbisProvider() || "auto");
    if (PROVIDERS.indexOf(provider) < 0) provider = "auto";
    var timeframe = loadLocal("cfg", "all", "x", "lasttf", "1h");
    var declaredTimeframes = null;
    var chartType = loadLocal("cfg", "all", "x", "lastctype", "velas");
    var refreshMode = loadLocal("cfg", "all", "x", "refresh", "auto");
    if (refreshMode !== "auto") {
      refreshMode = Number(refreshMode);
      if (REFRESH_MANUAL_VALUES.indexOf(refreshMode) < 0) refreshMode = "auto";
    }
    var refreshMs = refreshMode === "auto" ? 0 : Number(refreshMode);
    function effectiveRefreshMs() {
      if (refreshMode === "auto") return autoRefreshFor(timeframe);
      return Number(refreshMode) || 0; /* 0 = Apagado: sin temporizador */
    }
    var drawings = [];
    var undoStack = [], redoStack = [];
    var indCfg = sanitizeIndCfg(loadLocal("__ind", provider, timeframe, "indicators",
      { sma: { on: false, p: 20 }, ema: { on: false, p: 20 }, bb: { on: false, p: 20, m: 2 }, rsi: { on: false, p: 14 }, macd: { on: false, f: 12, s: 26, sig: 9 } }));
    /* Interruptores globales (Fase 5.3, hueco detectado): el imán y los
     * marcadores PAPER/DCA se perdían al recargar (siempre nacían activos).
     * Ahora persisten en local con el mismo patrón global que el refresco
     * (cfg|all|x, sin PUT: son preferencias de esta vista, como refresh).
     * showPlans sigue a showDCA (una sola casilla para DCA/planes). */
    var showPAPER = loadLocal("cfg", "all", "x", "showpaper", true) !== false,
      showDCA = loadLocal("cfg", "all", "x", "showdca", true) !== false,
      showPlans = showDCA;
    /* ---- Colores de velas personalizables (Fase 2, Bloque B) ----
     * Global (una sola preferencia, no por mercado/TF/proveedor): se aplica
     * al cargar Estudio y al cambiar de par, temporalidad o fuente, y
     * sobrevive a recargar (localStorage + /ux/state kind=candles).
     * wickAuto=true (defecto) = mechas del color del cuerpo, idéntico al
     * aspecto actual del motor; con mecha manual se fija la misma para
     * alcista y bajista (el motor acepta ambas; aquí una sola, 5 valores).
     * El verde/rojo por defecto usa los tonos del propio motor (#26a69a /
     * #ef5350): sin preferencia guardada el gráfico se ve como siempre. */
    var CANDLE_PRESETS = {
      "verde-rojo": { up: "#26a69a", down: "#ef5350", borderUp: "#26a69a", borderDown: "#ef5350", wick: "", wickAuto: true },
      "azul-rojo": { up: "#2962ff", down: "#f23645", borderUp: "#2962ff", borderDown: "#f23645", wick: "", wickAuto: true },
      "blanco-negro": { up: "#ffffff", down: "#000000", borderUp: "#ffffff", borderDown: "#ffffff", wick: "#e8e8e8", wickAuto: false }
    };
    function sanitizeCandles(v) {
      var d = CANDLE_PRESETS["verde-rojo"];
      var o = (v && typeof v === "object") ? v : {};
      function hex(k) { return isValidHexColor(o[k]) ? String(o[k]).toLowerCase() : d[k]; }
      return { up: hex("up"), down: hex("down"), borderUp: hex("borderUp"), borderDown: hex("borderDown"), wick: isValidHexColor(o.wick) ? String(o.wick).toLowerCase() : "", wickAuto: o.wickAuto !== false };
    }
    var candleCfg = sanitizeCandles(loadLocal("cfg", "all", "x", "candles", null));
    function persistCandleColors() {
      saveLocal("cfg", "all", "x", "candles", candleCfg);
      U.api("/ux/state", { method: "PUT", body: JSON.stringify({ market: "__candles", provider: "all", timeframe: "x", kind: "candles", payload: candleCfg }) }).catch(function () {});
    }
    /* #RRGGBB + alfa -> rgba() para el volumen (siempre legible sobre el
     * fondo #08100f: usa los bordes, no los cuerpos; en blanco/negro el
     * cuerpo bajista es negro y su borde blanco). */
    function hexA(hex, a) {
      var m = /^#([0-9a-fA-F]{6})$/.exec(String(hex || ""));
      if (!m) return "rgba(63,185,80," + a + ")";
      var v = parseInt(m[1], 16);
      return "rgba(" + ((v >> 16) & 255) + "," + ((v >> 8) & 255) + "," + (v & 255) + "," + a + ")";
    }
    function candleWicks() {
      if (candleCfg.wickAuto || !isValidHexColor(candleCfg.wick)) return [candleCfg.up, candleCfg.down];
      return [candleCfg.wick, candleCfg.wick];
    }
    function applyCandleColors() {
      if (!priceSeries) return;
      if (chartType !== "velas" && chartType !== "heikin" && chartType !== "barras") return;
      var w = candleWicks();
      try {
        priceSeries.applyOptions({ upColor: candleCfg.up, downColor: candleCfg.down, borderUpColor: candleCfg.borderUp, borderDownColor: candleCfg.borderDown, wickUpColor: w[0], wickDownColor: w[1], borderVisible: true, wickVisible: true });
      } catch (e) {}
      repaintVolumeColors();
    }
    function volRows() {
      return candles.map(function (c) {
        return { time: c.time, value: num(c.v), color: num(c.close) >= num(c.open) ? hexA(candleCfg.borderUp, 0.5) : hexA(candleCfg.borderDown, 0.5) };
      });
    }
    function repaintVolumeColors() {
      if (!volSeries || !candles.length) return;
      try { volSeries.setData(volRows()); } catch (e) {}
    }
    function paintCandles() {
      var box = $("st-candles");
      if (!box) return;
      function opt(key, label, val) {
        return '<label class="kbd cd-cell"><span class="cd-lab">' + label + '</span> <input type="color" id="cd-' + key + '" value="' + U.esc(val) + '" aria-label="' + label + '"></label>';
      }
      var wickVal = isValidHexColor(candleCfg.wick) ? candleCfg.wick : candleCfg.up;
      box.innerHTML =
        '<div class="row">' +
        '<button type="button" class="ghost" data-cpreset="verde-rojo">Verde/rojo</button>' +
        '<button type="button" class="ghost" data-cpreset="azul-rojo">Azul/rojo</button>' +
        '<button type="button" class="ghost" data-cpreset="blanco-negro">Blanco/negro</button></div>' +
        /* R3: rejilla 2×2 etiqueta-encima-muestra (las etiquetas no caben de
         * lado a 290 px) + fila propia de mecha con su checkbox. Muestras del
         * mismo tamaño y alineadas; sin partir palabras. */
        '<div class="cd-grid">' +
        "<div>" + opt("up", "Alcista", candleCfg.up) + "</div>" +
        "<div>" + opt("down", "Bajista", candleCfg.down) + "</div>" +
        "<div>" + opt("bup", "Borde alcista", candleCfg.borderUp) + "</div>" +
        "<div>" + opt("bdown", "Borde bajista", candleCfg.borderDown) + "</div>" +
        '<div class="cd-wickrow">' + opt("wick", "Mecha", wickVal) + ' <label class="kbd cd-check"><input type="checkbox" id="cd-wickauto"' + (candleCfg.wickAuto ? " checked" : "") + "><span>Mecha del color del cuerpo</span></label></div>" +
        "</div>" +
        '<div class="row"><button type="button" class="ghost" id="cd-reset">Restablecer</button></div>' +
        '<p class="muted">Se aplican al cambiar de mercado, temporalidad o fuente, y se conservan al recargar.</p>';
      var map = { up: "up", down: "down", bup: "borderUp", bdown: "borderDown", wick: "wick" };
      Object.keys(map).forEach(function (id) {
        var el = box.querySelector("#cd-" + id);
        if (el) el.addEventListener("change", function () {
          if (!isValidHexColor(el.value)) return;
          candleCfg[map[id]] = String(el.value).toLowerCase();
          if (id === "wick") {
            candleCfg.wickAuto = false;
            var wa2 = box.querySelector("#cd-wickauto");
            if (wa2) wa2.checked = false;
          }
          persistCandleColors(); applyCandleColors();
        });
      });
      var wa = box.querySelector("#cd-wickauto");
      if (wa) wa.addEventListener("change", function () {
        candleCfg.wickAuto = wa.checked;
        persistCandleColors(); applyCandleColors();
      });
      box.querySelectorAll("[data-cpreset]").forEach(function (b) {
        b.addEventListener("click", function () {
          var p = CANDLE_PRESETS[b.getAttribute("data-cpreset")];
          if (!p) return;
          candleCfg = JSON.parse(JSON.stringify(p));
          persistCandleColors(); applyCandleColors(); paintCandles();
        });
      });
      var rs = box.querySelector("#cd-reset");
      if (rs) rs.addEventListener("click", function () {
        candleCfg = JSON.parse(JSON.stringify(CANDLE_PRESETS["verde-rojo"]));
        persistCandleColors(); applyCandleColors(); paintCandles();
      });
    }
    var activeTool = null, selectedId = null,
      magnet = loadLocal("cfg", "all", "x", "magnet", true) !== false;
    var compareSymbol = null;
    var fibLevels = FIB_DEFAULT.slice();
    var layouts = {};
    var curLayout = null;
    var chart = null, priceSeries = null, volSeries = null, cmpSeries = null;
    var indSeries = {};
    /* Meta por clave de indSeries (Fase 5.1): el pane con el que se creó la
     * serie. Se resetea en los mismos 3 sitios que indSeries (chart.remove).
     * El color/título van en applyOptions y nunca exigen recrear. */
    var indSeriesMeta = {};
    /* Marcadores LWC v5: cada createSeriesMarkers adjunta un primitivo NUEVO
     * a la serie (llamarlo en cada pintada duplica y fuga). Se mantiene UNA
     * única instancia por serie (priceMarkers) y se actualiza con
     * setMarkers([...]) al llegar cada fuente async (PAPER y planes). */
    var priceMarkers = null, paperMarks = [], planMarks = [], paperOps = [];
    var paperBarGroups = Object.create(null);
    var paperMarkerRequestSeq = 0;
    /* M3: líneas de entrada + PnL flotante (estilo Binance/Bitget). Cada
     * createPriceLine adjunta una línea NUEVA a la serie (llamarlo en cada
     * tick duplica y fuga): se mantiene UNA única instancia por línea
     * (entryLine en el coste medio, pnlLine en el precio vivo) y se
     * actualiza con applyOptions; antes de recrear siempre removePriceLine
     * (mismo patrón anti-acumulación que A2/marcadores). entryPlan es el
     * plan cacheado { market, averageCost, pnlPct, pnlAbs } del portfolio;
     * se resetea en los mismos sitios que priceMarkers (chart.remove). */
    var entryPlan = null, entryLine = null, pnlLine = null;
    /* lastMouse: última posición del puntero (clientX/Y). El callback del
     * crosshair incluye param.point; se toma ahí para que la ficha aparezca
     * ya en el primer movimiento, antes del bubbling de mousemove. */
    var lastMouse = null;
    var candles = [], loading = false, hasMore = true, loadGeneration = 0;
    var ws = null, wsAttempt = 0, pollTimer = null, stopping = false, wsTickSeq = 0;
    var lastPrice = null, lastBid = null, lastAsk = null;
    /* El crosshair inspecciona velas históricas; nunca fija el precio de una
     * operación PAPER actual. La cotización operable conserva procedencia y
     * hora por separado de lastPrice, que también puede venir de una vela. */
    var lastCrossPrice = null, tradeSide = null;
    var lastTradeQuote = null, lastTradeQuoteReceivedMs = null;
    var pendingTradeRequest = null;
    /* M4: disponible para vender (solo informativo, del portfolio; null si
     * aún no se sabe: el Guard del servidor decide de verdad). */
    var lastAvailQty = null;
    /* M4: proveedor resuelto del último snapshot (provenance exchange del
     * helper compartido cuando la fuente es automática). */
    var lastResolvedProvider = null;
    var chartResolvedProvider = null; /* fuente de las velas, no del ticker */
    /* --- Timer propio Estudio (independiente) --- */
    var estudioRefreshTimer = null;
    var archiveRequested = false;
    var lastHistoryGesture = 0;
    /* Handlers/timers a nivel window/document: se guardan para eliminarlos
     * en cleanup() sin fugas al entrar/salir/reentrar a Estudio. */
    var winResizeHandler = null, repaintTimer = null, keyHandler = null;

    /* Los id st-*, data-tool/data-stf y aria-* son contratos con este módulo
     * y con las pruebas; se mantienen aunque las clases y los textos visibles
     * usen nombres descriptivos en español. */
    root.innerHTML =
      '<section class="card studio" aria-label="Estudio de mercado"><div class="estudio-cabecera"><h2 data-help="Mercados, gráficos, dibujos y operaciones PAPER. Las compras reales se registran en Cartera REAL.">Estudio</h2></div>' + U.modeTag() +
      '<div class="stoolbar" role="toolbar" aria-label="Ajustes del estudio">' +
      '<div class="st-market-field"><label class="field" for="st-pair">Mercado</label><input id="st-pair" value="' + U.esc(market) + '" autocomplete="off" spellcheck="false" aria-controls="st-search" aria-expanded="false" aria-autocomplete="list"><div id="st-search" class="st-market-results" role="group" aria-label="Mercados encontrados" hidden></div></div>' +
      '<label class="field">Fuente <select id="st-prov" aria-label="Fuente de precios del Estudio">' +
      PROVIDERS.map(function (p) { return '<option value="' + p + '"' + (p === provider ? " selected" : "") + ">" + U.esc(p === "auto" ? "Automática" : p.toUpperCase()) + "</option>"; }).join("") + "</select></label>" +
      '<label class="field">Intervalo <select id="st-tf">' +
      TF_LIST.map(function (t) { return '<option value="' + t + '"' + (t === timeframe ? " selected" : "") + ">" + t + "</option>"; }).join("") + "</select></label>" +
      '<div class="st-range-control"><label class="field" data-help="El periodo conserva el intervalo elegido. Para el archivo diario de Bitstamp, elige 1d y fuente Automática o Bitstamp.">Periodo visible <select id="st-range" aria-label="Periodo visible del gráfico"><option value="current">Últimas velas</option><option value="1">1 año</option><option value="5">5 años</option><option value="10">10 años</option><option value="all">Todo disponible</option><option value="custom">Fechas…</option></select></label>' +
      '<button type="button" id="st-range-edit" class="ghost st-range-edit" hidden aria-controls="st-range-dates" aria-expanded="false">Cambiar fechas</button>' +
      '<div id="st-range-dates" class="st-range-dates" hidden role="group" aria-label="Elegir fechas del gráfico"><div class="st-range-dates-fields"><label class="field">Desde<input id="st-range-from" type="date" aria-label="Desde fecha del gráfico"></label><label class="field">Hasta<input id="st-range-to" type="date" aria-label="Hasta fecha del gráfico"></label></div><p id="st-range-error" class="amber" role="status" hidden></p><div class="st-range-dates-actions"><button type="button" id="st-range-cancel" class="ghost">Cancelar</button><button type="button" id="st-range-apply">Aplicar</button></div></div></div>' +
      '<label class="field" data-help="Heikin-Ashi transforma solo el dibujo de las velas. Indicadores, señales y órdenes siguen usando precios reales.">Gráfico <select id="st-ctype">' +
      CHART_TYPES.map(function (c) { return '<option value="' + c[0] + '"' + (c[0] === chartType ? " selected" : "") + ">" + c[1] + "</option>"; }).join("") + "</select></label>" +
      '<details class="st-advanced"><summary data-help="Actualización y posición de la barra de dibujo.">Ajustes</summary><div class="st-advanced-grid">' +
      '<label class="field">Actualización <select id="st-refresh">' +
      '<option value="auto"' + (refreshMode === "auto" ? " selected" : "") + '>Automática (' + fmtRefreshMs(autoRefreshFor(timeframe)) + ")</option>" +
      '<option value="1000"' + (refreshMode === 1000 ? " selected" : "") + '>1 s</option>' +
      '<option value="5000"' + (refreshMode === 5000 ? " selected" : "") + '>5 s</option>' +
      '<option value="15000"' + (refreshMode === 15000 ? " selected" : "") + '>15 s</option>' +
      '<option value="60000"' + (refreshMode === 60000 ? " selected" : "") + '>1 min</option>' +
      '<option value="300000"' + (refreshMode === 300000 ? " selected" : "") + '>5 min</option>' +
      '<option value="0"' + (refreshMode === 0 ? " selected" : "") + '>Apagada</option></select></label>' +
      /* La posición de la barra de dibujo es un ajuste más de esta fila, no un
       * plegable aparte: evita una fila entera ocupada solo por un título. */
      '<label class="field st-cfg-tools" id="st-cfg-tools" title="Dónde se coloca la barra de herramientas de dibujo">Barra de dibujo <select id="st-cfg-dock">' +
      DOCK_OPTS.map(function (o) { return '<option value="' + o + '">' + o.charAt(0).toUpperCase() + o.slice(1) + "</option>"; }).join("") + "</select></label></div></details>" +
      '<div class="acciones-grafico">' +
      '<span id="st-refresh-note" class="muted" role="status"></span>' +
      '<button type="button" id="st-load" data-help="Consulta nuevas velas de la fuente y el intervalo seleccionados.">Actualizar</button>' +
      '<button type="button" id="st-save" class="ghost" data-help="Guarda mercado, gráfico, indicadores y dibujos de este espacio.">Guardar</button>' +
      '<button type="button" id="st-full" class="ghost" data-help="Amplía el gráfico dentro de la ventana.">Ampliar</button>' +
      '<details class="st-view-actions"><summary aria-label="Opciones de vista del gráfico">Vista</summary><div class="st-view-actions-menu" role="group" aria-label="Navegación del gráfico">' +
      '<label id="st-archive-year-wrap" class="field" hidden data-help="Salta a un año del archivo BTC diario; no cambia los datos ni la fuente.">Ir al año<select id="st-archive-year" aria-label="Año del archivo BTC"><option value="">Elegir</option></select></label>' +
      '<button type="button" id="st-reset" class="ghost" title="Restablece el acercamiento y centra la vista">Centrar</button>' +
      '<button type="button" id="st-zin" class="ghost" title="Acercar el gráfico">Acercar +</button>' +
      '<button type="button" id="st-zout" class="ghost" title="Alejar el gráfico">Alejar −</button>' +
      '</div></details>' +
      '</div></div>' +
      '<div class="st-basic-status"><span id="st-tf-note" class="muted" role="status"></span></div>' +
      '<div class="stgrid"><div class="principal-estudio">' +
      '<div id="st-meta" aria-live="off"></div>' +
      '<span id="st-quality" class="amber" role="status" hidden></span>' +
      '<div id="st-archive-meta" aria-live="polite"></div>' +
      '<div id="st-loading" class="muted" hidden>Cargando más datos… (vista conservada)</div>' +
      '<div class="stchartdock dock-left" id="st-dockwrap">' +
      '<div class="drawtools dock-left" id="st-drawtools" role="toolbar" aria-label="Herramientas de dibujo">' +
      '<button type="button" class="ghost tool" data-tool="h" aria-pressed="false" title="Línea horizontal de precio (un toque)"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><line x1="1" y1="8" x2="15" y2="8"/><rect x="10.5" y="5" width="4" height="6" fill="none"/></svg></span><span class="tlabel">Precio</span></button>' +
      '<button type="button" class="ghost tool" data-tool="trend" aria-pressed="false" title="Línea de tendencia diagonal con 2 puntos (arrastra de A a B)"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><line x1="2.5" y1="13" x2="13.5" y2="3"/><circle cx="2.5" cy="13" r="1.7"/><circle cx="13.5" cy="3" r="1.7"/></svg></span><span class="tlabel">Tendencia</span></button>' +
      '<button type="button" class="ghost tool" data-tool="v" aria-pressed="false" title="Línea vertical en tiempo (1 clic)"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><line x1="8" y1="1.5" x2="8" y2="14.5"/><path d="M5.5 4.5 8 2l2.5 2.5"/></svg></span><span class="tlabel">Vertical</span></button>' +
      '<button type="button" class="ghost tool" data-tool="rect" aria-pressed="false" title="Zona rectangular (arrastra de A a B)"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><rect x="2.5" y="4" width="11" height="8"/></svg></span><span class="tlabel">Zona</span></button>' +
      '<button type="button" class="ghost tool" data-tool="text" aria-pressed="false" title="Texto en el gráfico (1 clic, pide el texto)"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><text x="4" y="12.5" font-size="11" font-weight="700">T</text></svg></span><span class="tlabel">Texto</span></button>' +
      '<button type="button" class="ghost tool" data-tool="measure" aria-pressed="false" title="Medidor de distancia y porcentaje (arrastra de A a B)"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><path d="M2.5 13.5 13.5 2.5"/><path d="M5 11.2l1.8 1.8M8 8.2l1.8 1.8M11 5.2l1.8 1.8"/></svg></span><span class="tlabel">Medidor</span></button>' +
      '<button type="button" class="ghost tool" data-tool="long" aria-pressed="false" title="Escenario alcista con entrada, límite y objetivo"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><path d="M8 13.5V3.5M4.5 7 8 3.5 11.5 7" stroke="#42c88a"/></svg></span><span class="tlabel">Alcista</span></button>' +
      '<button type="button" class="ghost tool" data-tool="short" aria-pressed="false" title="Escenario bajista con entrada, límite y objetivo"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><path d="M8 2.5v10M4.5 9 8 12.5 11.5 9" stroke="#ff7b78"/></svg></span><span class="tlabel">Bajista</span></button>' +
      '<button type="button" class="ghost tool" data-tool="fib" aria-pressed="false" title="Retroceso de Fibonacci con niveles (arrastra de A a B)"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><line x1="3" y1="2" x2="3" y2="14"/><line x1="3" y1="5" x2="13.5" y2="5"/><line x1="3" y1="8.5" x2="13.5" y2="8.5"/><line x1="3" y1="12" x2="13.5" y2="12"/></svg></span><span class="tlabel">Fib</span></button>' +
      '<button type="button" class="ghost tool" data-tool="fibext" aria-pressed="false" title="Extensión/proyección de Fibonacci: arrastra A→B y luego clic en C"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><line x1="2" y1="11" x2="8" y2="6"/><line x1="8" y1="6" x2="14" y2="9.5" stroke-dasharray="2 1.5"/><line x1="8" y1="12.5" x2="14" y2="12.5"/></svg></span><span class="tlabel">Fib ext</span></button>' +
      '<button type="button" class="ghost tool" data-tool="alertline" aria-pressed="false" title="Línea de alerta con campana (1 clic, crea alerta de precio)"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><path d="M8 1.8a3.6 3.6 0 0 1 3.6 3.6v2.8l1.2 2.1H3.2L4.4 8.2V5.4A3.6 3.6 0 0 1 8 1.8z"/><circle cx="8" cy="12.8" r="1.3"/></svg></span><span class="tlabel">Alerta</span></button>' +
      '<button type="button" class="ghost tool" data-tool="freehand" aria-pressed="false" title="Trazo libre: mantén pulsado y dibuja a mano alzada (al soltar queda guardado)"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><path d="M11.6 2.2l2.2 2.2L5.4 12.8l-3 1 1-3z"/><path d="M10 3.8l2.2 2.2"/></svg></span><span class="tlabel">Trazo</span></button>' +
      '<button type="button" class="ghost tool" id="st-undo" title="Deshacer último dibujo (Ctrl+Z)"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><path d="M6.5 3.5 3 7l3.5 3.5"/><path d="M3.6 7H10a3.2 3.2 0 0 1 0 6.4H6.5"/></svg></span><span class="tlabel">Deshacer</span></button>' +
      '<button type="button" class="ghost tool" id="st-redo" title="Rehacer dibujo (Ctrl+Mayús+Z)"><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><path d="M9.5 3.5 13 7l-3.5 3.5"/><path d="M12.4 7H6a3.2 3.2 0 0 0 0 6.4h3.5"/></svg></span><span class="tlabel">Rehacer</span></button>' +
      '<label class="kbd magnetlbl" title="Ajusta cada punto al valor más cercano de la vela"><input type="checkbox" id="st-magnet"' + (magnet ? " checked" : "") + '><span class="tic" aria-hidden="true"><svg viewBox="0 0 16 16"><path d="M4 1.8v6.4a4 4 0 0 0 8 0V1.8"/><path d="M4 1.8h2.8v3.6H4zM9.2 1.8H12v3.6H9.2z"/></svg></span> Ajustar a velas</label></div>' +
      '<div id="st-wrap" style="position:relative"><div id="st-chart"></div>' +
      /* R2: z-index 5 = por encima de los canvas del motor (z-index 1/2) y de
       * su logo (3), por debajo del arrastre de panes (49/50) y del panel
       * flotante (100). Sin z-index propio el SVG quedaba debajo aunque vaya
       * después en el DOM, y los dibujos eran invisibles. pointer-events:none
       * intacto: los clics siguen llegando al gráfico y a la leyenda. */
      '<svg id="st-draw" style="position:absolute;inset:0;width:100%;z-index:5;pointer-events:none" aria-hidden="true"></svg>' +
      '<div id="st-tip" class="ctxmenu" hidden></div>' +
      /* M1: tooltip propio de operaciones PAPER. El motor v5.0.9 ignora el
       * campo `title` de los marcadores, así que la información (fecha,
       * lado, cantidad, precio, comisión) vive en esta capa, dentro de
       * #st-wrap (position:relative) junto al gráfico y a #st-ohlc.
       * pointer-events:none: nunca bloquea el dibujo ni la navegación. */
      '<div id="st-marker-tip" class="st-marker-tip" hidden></div></div>' +
      "</div>" +
      '<div id="st-ohlc" class="muted" aria-live="off"></div>' +
      '<div id="st-paper-marks-status" class="amber" role="status" hidden></div>' +
      '<details class="det panel-lateral st-objects-tray"><summary>Objetos dibujados</summary><div id="st-objects"></div></details>' +
      /* La columna lateral lleva el estado actual del mercado, la lista de
       * mercados seguidos y las funciones secundarias. El gráfico y su
       * lectura se quedan arriba a la izquierda, como protagonistas. */
      "</div><aside class=\"stside\">" +
      '<section class="panel-dato" aria-label="Compra y venta">' +
      '<h3 class="panel-titulo" data-help="Órdenes simuladas en PAPER; no ejecuta operaciones reales.">Operar · PAPER</h3>' +
      '<div id="st-depth" class="dato-mercado">—</div>' +
      '<p id="st-price-source" class="st-price-source muted" role="status" hidden></p>' +
      /* El formulario PAPER usa sólo una cotización actual del par visible;
       * el cursor del gráfico queda reservado a la inspección histórica. */
      '<p class="muted" id="st-trade-pair" aria-live="off"></p>' +
      '<div class="row"><button type="button" id="st-buy-open" class="ghost" data-help="Abre el formulario de compra ficticia en PAPER.">Comprar</button>' +
      '<button type="button" id="st-sell-open" class="ghost" data-help="Abre el formulario de venta ficticia en PAPER.">Vender</button></div>' +
      '<form id="st-trade-form" hidden><div class="row">' +
      '<p class="muted" id="st-trade-side" aria-live="off"></p>' +
      '<label class="field"><span id="st-trade-qtylab">Importe</span> <input id="st-trade-qty" inputmode="decimal" required></label>' +
      '<label class="field" data-help="Referencia pública reciente. El servidor fija el precio PAPER al ask/bid disponible o, si falta, al último precio aproximado. La vela bajo el cursor no se usa.">Referencia <input id="st-trade-price" inputmode="decimal" readonly></label>' +
      '<button type="submit" id="st-trade-send" data-help="Registra la operación ficticia con el precio confirmado por el servidor.">Confirmar</button>' +
      '<button type="button" id="st-trade-cancel" class="ghost">Cancelar</button></div></form>' +
      '<div id="st-trade-msg" aria-live="off"></div></section>' +
      '<details class="det panel-lateral" open><summary>Mercados seguidos</summary>' +
      '<div class="row"><input id="st-wladd" aria-label="Mercado para seguir" placeholder="BTC/EUR">' +
      '<button type="button" id="st-wlpush" class="ghost" data-help="Añade este mercado a la lista de seguimiento local.">Añadir</button></div>' +
      '<ul class="wl" id="st-wl"></ul></details>' +
      '<div class="det studio-functions" id="st-functions"><div class="st-func-title" id="st-func-title">Análisis y organización</div>' +
      '<div class="st-func-menu" role="group" aria-labelledby="st-func-title">' +
      '<button type="button" class="ghost st-func-btn" data-stf="layouts" aria-expanded="false" aria-controls="stf-panel-layouts" data-help="Guarda y recupera mercados, indicadores y dibujos." aria-label="Espacios guardados">Espacios</button>' +
      '<button type="button" class="ghost st-func-btn" data-stf="inds" aria-expanded="false" aria-controls="stf-panel-inds" data-help="Añade indicadores al gráfico.">Indicadores</button>' +
      '<button type="button" class="ghost st-func-btn" data-stf="cmp" aria-expanded="false" aria-controls="stf-panel-cmp" data-help="Superpone otro mercado en este gráfico." aria-label="Comparar mercados">Comparar</button>' +
      '<button type="button" class="ghost st-func-btn" data-stf="alerts" aria-expanded="false" aria-controls="stf-panel-alerts" data-help="Crea alertas para niveles de precio." aria-label="Alertas de precio">Alertas</button>' +
      '<button type="button" class="ghost st-func-btn" data-stf="marks" aria-expanded="false" aria-controls="stf-panel-marks" data-help="Muestra u oculta marcas PAPER y DCA en el gráfico.">Señales</button>' +
      '<button type="button" class="ghost st-func-btn" data-stf="candles" aria-expanded="false" aria-controls="stf-panel-candles" data-help="Configura los colores de las velas.">Velas</button>' +
      "</div>" +
      '<div class="st-func-panels">' +
      '<section class="st-func-panel" id="stf-panel-layouts" aria-label="Espacios guardados" hidden><div class="row">' +
      '<label class="field">Espacio <select id="st-layoutsel"></select></label>' +
      '<label class="field">Nombre <input id="st-layoutname" placeholder="Estrategia A"></label>' +
      '<button type="button" id="st-layoutsave" class="ghost">Guardar como</button>' +
      '<button type="button" id="st-layoutdup" class="ghost">Duplicar</button>' +
      '<button type="button" id="st-layoutdel" class="ghost">Eliminar</button></div>' +
      '<p class="muted" id="st-layoutmsg">Cada espacio conserva el mercado, el gráfico, los indicadores y los dibujos.</p></section>' +
      '<section class="st-func-panel" id="stf-panel-inds" aria-label="Indicadores" hidden><div id="st-inds"></div></section>' +
      '<section class="st-func-panel" id="stf-panel-cmp" aria-label="Comparar" hidden><div class="row"><label class="field">Par <input id="st-cmp" placeholder="ETH/EUR"></label><button type="button" id="st-cmpadd" class="ghost">Comparar</button><button type="button" id="st-cmpdel" class="ghost">Quitar</button></div></section>' +
      '<section class="st-func-panel" id="stf-panel-alerts" aria-label="Alertas de precio" hidden><div class="row"><select id="st-alop" aria-label="Condición de alerta"><option value="gt">&gt;</option><option value="lt">&lt;</option></select><label class="field">Precio <input id="st-alprice" inputmode="decimal"></label><button type="button" id="st-aladd" class="ghost">Añadir alerta</button> <a href="#/alertas">Gestionar alertas</a></div><div id="st-alerts"></div></section>' +
      '<section class="st-func-panel" id="stf-panel-marks" aria-label="Marcadores" hidden><div class="row">' +
      '<label class="kbd"><input type="checkbox" id="st-mkpaper"' + (showPAPER ? " checked" : "") + '> PAPER</label>' +
      '<label class="kbd"><input type="checkbox" id="st-mkdca"' + (showDCA ? " checked" : "") + '> DCA/planes</label></div></section>' +
      '<section class="st-func-panel" id="stf-panel-candles" aria-label="Colores de las velas" hidden><div id="st-candles"></div></section>' +
      "</div></div>" +
      '<details class="det st-help panel-lateral"><summary>Cómo usar el Estudio</summary><div class="st-help-body">' +
      '<details class="st-help-sec"><summary>1. Navegar el gráfico</summary><p class="kbd">Rueda: acerca y aleja centrado en el puntero. Mayús+rueda o arrastrar: recorre el histórico. Arrastra el eje de precios para fijar la escala; doble clic en el eje o Centrar la devuelven a automática. Acercar, Alejar y Centrar también están en los botones.</p></details>' +
      '<details class="st-help-sec"><summary>2. Herramientas de dibujo</summary><p class="kbd">Para Tendencia, Retroceso, Zona, Medidor, Alcista y Bajista, arrastra de A a B. Trazo libre sigue el puntero mientras mantienes pulsado. Precio, Vertical, Texto y Alerta se colocan con un toque. Proyección y los escenarios usan un segundo toque para completar el objetivo.</p></details>' +
      '<details class="st-help-sec"><summary>3. Seleccionar y configurar un objeto</summary><p class="kbd">Toca un objeto sin tener una herramienta activa para seleccionarlo. Arrastra su cuerpo para moverlo entero o tira de sus puntos de control para cambiar un extremo. Podrás cambiar sus niveles, texto, color, grosor, estilo, opacidad y posición, duplicarlo, o bloquearlo, ocultarlo y eliminarlo.</p></details>' +
      '<details class="st-help-sec"><summary>4. Preferencias de herramientas</summary><p class="kbd">Cambia la ubicación de la barra sobre el gráfico. Ajustar a velas lleva cada punto al valor más cercano.</p></details>' +
      '<details class="st-help-sec"><summary>5. Atajos de teclado</summary><p class="kbd">Esc cancela la herramienta. Supr borra el objeto seleccionado. Ctrl+Z deshace y Ctrl+Mayús+Z rehace.</p></details>' +
      '<details class="st-help-sec"><summary>6. Alertas de precio y sonido</summary><p class="kbd">Crea alertas de precio desde el panel o con la herramienta Alerta; cuando se cumple la condición aparece un aviso. El sonido se puede silenciar en Configuración.</p></details>' +
      "</div></details>" +
      "</aside></div></section>";

    var $ = function (id) { return root.querySelector("#" + id); };
    var savedRange = loadLocal("cfg", "all", "x", "visibleRange", "current");
    if (["current", "1", "5", "10", "all", "custom"].includes(savedRange)) $("st-range").value = savedRange;
    function dateValue(value) { return /^\d{4}-\d{2}-\d{2}$/.test(String(value || "")) ? value : ""; }
    $("st-range-from").value = dateValue(loadLocal("cfg", "all", "x", "rangeFrom", ""));
    $("st-range-to").value = dateValue(loadLocal("cfg", "all", "x", "rangeTo", ""));
    $("st-range-dates").hidden = true;
    $("st-range-edit").hidden = $("st-range").value !== "custom";
    var appliedRange = $("st-range").value;
    var chartBox = $("st-chart"), drawSvg = $("st-draw"), tip = $("st-tip");
    function noteHistoryGesture() { lastHistoryGesture = Date.now(); }
    chartBox.addEventListener("pointerdown", noteHistoryGesture, {capture:true});
    chartBox.addEventListener("pointermove", function (event) {
      if (event.buttons) noteHistoryGesture();
    }, {capture:true});
    chartBox.addEventListener("wheel", noteHistoryGesture, {capture:true, passive:true});

    function pushUndo() {
      undoStack.push(JSON.stringify(drawings));
      if (undoStack.length > 50) undoStack.shift();
      redoStack = [];
    }
    function doUndo() {
      if (!undoStack.length) return;
      redoStack.push(JSON.stringify(drawings));
      drawings = JSON.parse(undoStack.pop());
      persistDrawings(); redrawDrawings(); paintObjects();
    }
    function doRedo() {
      if (!redoStack.length) return;
      undoStack.push(JSON.stringify(drawings));
      drawings = JSON.parse(redoStack.pop());
      persistDrawings(); redrawDrawings(); paintObjects();
    }

    function stateKind(k) { return { market: market, provider: provider === "auto" ? "auto" : provider, timeframe: timeframe, kind: k }; }
    function persistDrawings() {
      saveLocal(market, provider, timeframe, "drawings", drawings);
      var st = stateKind("drawings");
      U.api("/ux/state", { method: "PUT", body: JSON.stringify({ market: st.market, provider: st.provider, timeframe: st.timeframe, kind: "drawings", payload: { items: drawings } }) }).catch(function () {});
    }
    function persistInd() {
      saveLocal("__ind", provider, timeframe, "indicators", indCfg);
      var st = stateKind("indicators");
      U.api("/ux/state", { method: "PUT", body: JSON.stringify({ market: st.market, provider: st.provider, timeframe: st.timeframe, kind: "indicators", payload: indCfg }) }).catch(function () {});
    }
    function loadPersisted() {
      drawings = loadLocal(market, provider, timeframe, "drawings", []);
      var st = stateKind("drawings");
      var requested = st.market + "|" + st.provider + "|" + st.timeframe;
      U.api("/ux/state?market=" + encodeURIComponent(market) + "&provider=" + encodeURIComponent(st.provider) + "&timeframe=" + encodeURIComponent(timeframe) + "&kind=drawings").then(function (r) {
        if (requested !== market + "|" + (provider === "auto" ? "auto" : provider) + "|" + timeframe) return;
        if (r.ok && r.data && r.data.payload && Array.isArray(r.data.payload.items) && !drawings.length) {
          drawings = r.data.payload.items.filter(function (d) { return d && d.market !== undefined ? d.market === market : true; });
          redrawDrawings(); paintObjects();
        }
      }).catch(function () {});
      loadFibLevels();
      /* Indicadores (reparación Fase 5: antes solo se hacía PUT, nunca GET,
       * así que en un navegador limpio no se restauraban). Mismo patrón que
       * dibujos: el local manda y el backend solo rellena si el local está
       * vacío; con guarda de contexto (mercado|proveedor|TF al pedir vs al
       * llegar la respuesta: si el usuario cambió mientras volaba, no se
       * aplica nada de otro contexto). Prioridad: local presente ⇒ se
       * conserva el local (ni se pide); backend sin nada ⇒ se conserva lo
       * que haya. Sin bucles de PUT: al aplicar NO se persiste (el backend
       * ya tiene ese valor; el próximo cambio del usuario lo guardará con
       * persistInd). Sin carreras con la carga normal: si el usuario tocó
       * un indicador mientras volaba, persistInd ya guardó el local y la
       * respuesta se descarta; paintInds/paintIndicators solo repintan con
       * lo restaurado y maybeRefillForIndicators pide más velas si el
       * periodo restaurado las necesita (igual que al cambiar a mano). Nota:
       * el local es por proveedor+TF compartido entre mercados (clave
       * "__ind", la misma de persistInd/saveLocal) mientras el backend
       * guarda por mercado real (igual que su PUT): el GET pregunta por el
       * mercado actual, coherente con lo que su propio PUT escribió. */
      (function () {
        var indLocalKey = persistenceKey("__ind", provider, timeframe, "indicators");
        try {
          if (localStorage.getItem(indLocalKey)) return;
        } catch (e) {}
        var ist = stateKind("indicators");
        var reqMarket = ist.market, reqProv = ist.provider, reqTf = ist.timeframe;
        U.api("/ux/state?market=" + encodeURIComponent(reqMarket) + "&provider=" + encodeURIComponent(reqProv) + "&timeframe=" + encodeURIComponent(reqTf) + "&kind=indicators").then(function (r) {
          if (reqMarket !== market || reqProv !== (provider === "auto" ? "auto" : provider) || reqTf !== timeframe) return;
          if (!(r.ok && r.data && shouldApplyIndPayload(r.data.payload))) return;
          try {
            if (localStorage.getItem(persistenceKey("__ind", provider, timeframe, "indicators"))) return;
          } catch (e) {}
          var clean = sanitizeIndCfg(r.data.payload);
          if (!clean) return;
          indCfg = clean;
          try { paintInds(); } catch (e) {}
          try { paintIndicators(); } catch (e) {}
          try { maybeRefillForIndicators(); } catch (e) {}
        }).catch(function () {});
      })();
      /* Colores de velas (global): el local manda; solo si no hay nada
       * guardado se pregunta al servidor (mismo patrón que dibujos). */
      try {
        if (!localStorage.getItem(persistenceKey("cfg", "all", "x", "candles"))) {
          U.api("/ux/state?market=__candles&provider=all&timeframe=x&kind=candles").then(function (r) {
            if (r.ok && r.data && r.data.payload && typeof r.data.payload === "object") {
              try {
                if (!localStorage.getItem(persistenceKey("cfg", "all", "x", "candles"))) {
                  candleCfg = sanitizeCandles(r.data.payload);
                  applyCandleColors(); paintCandles();
                }
              } catch (e) {}
            }
          }).catch(function () {});
        }
      } catch (e) {}
    }

    /* ---- 1) Niveles fib configurables por mercado+provider ----
     * Checkboxes 0/0.236/0.382/0.5/0.618/0.786/1 + custom (1.272/1.618...).
     * Se aplican a nuevos y al seleccionado; persisten en localStorage y
     * /ux/state kind=fiblevels con timeframe x (por mercado+provider). */
    function fibStore() { return { market: market, provider: (provider === "auto" ? "auto" : provider), timeframe: "x", kind: "fiblevels" }; }
    function loadFibLevels() {
      var fb = FIB_DEFAULT.slice();
      try {
        var raw = localStorage.getItem(persistenceKey(market, (provider === "auto" ? "auto" : provider), "x", "fiblevels"));
        if (raw) { var arr = JSON.parse(raw); if (Array.isArray(arr) && arr.length) fb = arr.map(Number).filter(isFinite); }
      } catch (e) {}
      fibLevels = fb.length ? fb : FIB_DEFAULT.slice();
      U.api("/ux/state?market=" + encodeURIComponent(market) + "&provider=" + encodeURIComponent(provider === "auto" ? "auto" : provider) + "&timeframe=x&kind=fiblevels").then(function (r) {
        if (r.ok && r.data && r.data.payload && Array.isArray(r.data.payload.levels) && r.data.payload.levels.length) {
          try {
            if (!localStorage.getItem(persistenceKey(market, (provider === "auto" ? "auto" : provider), "x", "fiblevels"))) {
              fibLevels = r.data.payload.levels.map(Number).filter(isFinite);
              paintFibPanel();
            }
          } catch (e) {}
        }
      }).catch(function () {});
    }
    function persistFibLevels() {
      try { localStorage.setItem(persistenceKey(market, (provider === "auto" ? "auto" : provider), "x", "fiblevels"), JSON.stringify(fibLevels)); } catch (e) {}
      var st = fibStore();
      U.api("/ux/state", { method: "PUT", body: JSON.stringify({ market: st.market, provider: st.provider, timeframe: st.timeframe, kind: "fiblevels", payload: { levels: fibLevels } }) }).catch(function () {});
    }
    function paintFibPanel() {
      var box = $("st-fiblevels");
      if (!box) return;
      var all = FIB_DEFAULT.concat(fibLevels.filter(function (v) { return FIB_DEFAULT.indexOf(Number(v)) < 0; }));
      all = all.map(Number).filter(isFinite).sort(function (a, b) { return a - b; });
      var html = "";
      all.forEach(function (L) {
        var on = fibLevels.indexOf(L) >= 0;
        html += '<label class="kbd"><input type="checkbox" data-fib="' + L + '"' + (on ? " checked" : "") + "> " + L + "</label> ";
      });
      html += '<p class="muted">Sugeridos custom: ' + FIB_SUGGEST.join(" · ") + ".</p>";
      box.innerHTML = html;
      box.querySelectorAll("[data-fib]").forEach(function (cb) {
        cb.addEventListener("change", function () {
          var v = Number(cb.getAttribute("data-fib"));
          if (cb.checked && fibLevels.indexOf(v) < 0) fibLevels.push(v);
          if (!cb.checked) fibLevels = fibLevels.filter(function (x) { return Number(x) !== v; });
          fibLevels.sort(function (a, b) { return a - b; });
          persistFibLevels();
          var sel = drawings.filter(function (o) { return o.id === selectedId && o.type === "fib"; })[0];
          if (sel) { sel.levels = fibLevels.slice(); persistDrawings(); redrawDrawings(); }
        });
      });
    }

    /* ---- 3) Layouts nombrados (p.ej. Estrategia A/B) ----
     * Cada uno guarda par/provider/timeframe/tipo/indicadores/dibujos/
     * watchlist parcial. Persistencia localStorage orbis.estudio.layouts +
     * /ux/state kind=layouts (market __layouts). Cambiar recarga todo sin
     * mezclar: los dibujos se filtran por mercado (nunca ETH en BTC). */
    function loadLayoutsFromLocal() {
      try {
        var raw = localStorage.getItem(LAYOUTS_KEY);
        if (raw) { var o = JSON.parse(raw); if (o && typeof o === "object") layouts = o; }
      } catch (e) {}
    }
    function persistLayouts() {
      try { localStorage.setItem(LAYOUTS_KEY, JSON.stringify(layouts)); } catch (e) {}
      U.api("/ux/state", { method: "PUT", body: JSON.stringify({ market: "__layouts", provider: "all", timeframe: "x", kind: "layouts", payload: { layouts: layouts, current: curLayout } }) }).catch(function () {});
    }
    function paintLayouts(msg) {
      var sel = $("st-layoutsel"), box = $("st-layoutmsg");
      if (!sel) return;
      var names = Object.keys(layouts).sort();
      sel.innerHTML = names.length
        ? names.map(function (n) { return '<option value="' + U.esc(n) + '"' + (n === curLayout ? " selected" : "") + ">" + U.esc(n) + "</option>"; }).join("")
        : '<option value="">(sin layouts)</option>';
      if (box && msg) box.textContent = msg;
    }
    function snapshotLayout(name) {
      var wl = [];
      try { wl = JSON.parse(localStorage.getItem(persistenceKey("__wl", "all", "x", "items")) || "[]"); } catch (e) { wl = []; }
      return { name: name, market: market, provider: provider, timeframe: timeframe, chartType: chartType, indCfg: JSON.parse(JSON.stringify(indCfg)), drawings: JSON.parse(JSON.stringify(drawings)), watchlist: (Array.isArray(wl) ? wl.slice(0, 20) : []) };
    }
    function applyLayout(name, silent) {
      var L = layouts[name];
      if (!L) return;
      loadGeneration++;
      loading = false;
      lastHistoryGesture = 0;
      stopStream();
      chartResolvedProvider = null;
      /* Sin estado a medias del layout anterior: aborta cualquier gesto de
       * dibujo en curso y restaura la navegación (ver Bloqueante 2). */
      cancelDrawingInteractions();
      curLayout = name;
      market = canon2(L.market || "BTC/EUR");
      provider = L.provider || "auto";
      timeframe = L.timeframe || "1h";
      chartType = L.chartType || "velas";
      try { indCfg = JSON.parse(JSON.stringify(L.indCfg || indCfg)); } catch (e) {}
      /* Sin mezclar: nunca dibujo ETH en BTC. */
      var want = canon2(L.market || market);
      drawings = (Array.isArray(L.drawings) ? L.drawings : []).filter(function (d) { return !d || d.market === undefined || d.market === want; });
      if ($("st-pair")) $("st-pair").value = market;
      if ($("st-prov")) $("st-prov").value = provider;
      if ($("st-tf")) $("st-tf").value = timeframe;
      if ($("st-ctype")) $("st-ctype").value = chartType;
      undoStack = []; redoStack = []; selectedId = null;
      persistDrawings(); persistInd(); persistLayouts();
      paintLayouts(silent ? null : ("Layout «" + name + "» cargado: " + market + " en " + provider + "."));
      paintInds(); paintCandles(); paintObjects();
      if (chart) { try { chart.remove(); } catch (e) {} chart = null; priceSeries = null; volSeries = null; cmpSeries = null; indSeries = {}; indSeriesMeta = {}; priceMarkers = null; paperMarks = []; planMarks = []; paperOps = []; paperBarGroups = Object.create(null); entryPlan = null; entryLine = null; pnlLine = null; /* M3 */ lastMouse = null; try { hideMarkerTip(); } catch (e) {} }
      chartBox.innerHTML = "";
      resetArchiveDisplay();
      candles = []; hasMore = true;
      closeTradeForm(); /* M4: el formulario es del layout anterior */
      lastTradeQuote = null; lastTradeQuoteReceivedMs = null;
      pendingTradeRequest = null;
      var tradeMsgBox2 = $("st-trade-msg");
      if (tradeMsgBox2) tradeMsgBox2.innerHTML = "";
      paintTradePair();
      refreshing = false; refreshLastClose = null;
      resetScaleMemory(); /* layout nuevo = escala automática de nuevo (R1) */
      ensureChart();
      loadCandles(false);
      restartEstudioTimer();
    }

    function ensureChart() {
      if (!window.LightweightCharts) {
        chartBox.innerHTML = '<div class="state empty" role="status">El motor del gráfico no está disponible. El resto del Estudio sigue funcionando.</div>';
        return false;
      }
      if (chart) return true;
      resetScaleMemory(); /* chart nuevo: memoria de escala a su valor inicial (R1) */
      lastCrossPrice = null; /* M4: el precargado siempre es del mercado visible */
      var LW = window.LightweightCharts;
      /* Navegación nativa (Fase 2): rueda = zoom anclado al puntero,
       * Mayús+rueda (deltaX) = desplazar, arrastre = mover, eje = escala,
       * doble clic en eje = auto. Verificado en fuente del vendor (Ym: deltaY
       * -> bc(x) zoom, deltaX -> Sc scroll) y en harness Node (deriva 0 px).
       * autoSize:false: con autoSize:true el resize(w,h) del motor es NO-OP
       * y el tamaño lo impone el observador; Estudio mide el contenedor y
       * llama a chart.resize(w,h) con anti-realimentación (ver fitChartSize). */
      /* 5.000+ velas diarias deben caber también en una ventana estrecha.
       * Con 0.1 px/barra y sin bloqueo de rango al redimensionar, el motor
       * recortaba "Todo disponible" (BTC 2012) aunque los datos estuviesen
       * cargados. El zoom manual conserva cada vela original. */
      chart = LW.createChart(chartBox, { layout: { background: { color: "#08100f" }, textColor: "#9caaa4" }, grid: { vertLines: { color: "#172521" }, horzLines: { color: "#172521" } }, crosshair: { mode: 0 }, autoSize: false, handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true }, handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true }, timeScale: { timeVisible: true, secondsVisible: timeframe === "1s" || timeframe === "30s", minBarSpacing: 0.01, lockVisibleTimeRangeOnResize: true }, rightPriceScale: { autoScale: true }, localization: { locale: "es-ES" } });
      try { chart.priceScale("right").applyOptions({ autoScale: true }); } catch (e) { /* motor ausente: honesto, sin romper */ }
      var dec = precision(market);
      var pf = { type: "price", precision: dec, minMove: 1 / Math.pow(10, dec) };
      if (chartType === "velas" || chartType === "heikin") priceSeries = chart.addSeries(LW.CandlestickSeries, { priceFormat: pf });
      else if (chartType === "linea") priceSeries = chart.addSeries(LW.LineSeries, { color: "#4da3ff", priceFormat: pf });
      else if (chartType === "area") priceSeries = chart.addSeries(LW.AreaSeries, { priceFormat: pf });
      else priceSeries = chart.addSeries(LW.BarSeries, { priceFormat: pf });
      volSeries = chart.addSeries(LW.HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" } }, 1);
      try { chart.panes()[1].setHeight(110); } catch (e) {}
      chart.subscribeCrosshairMove(function (param) {
        /* M1: sin tiempo (puntero fuera del gráfico) se oculta el tooltip
         * PAPER junto con la leyenda: no queda ninguna tarjeta colgada. */
        if (!param || param.time === undefined || param.time === null) { hideMarkerTip(); return; }
        if (param.point && isFinite(param.point.x) && isFinite(param.point.y)) {
          var chartRect = chartBox.getBoundingClientRect();
          lastMouse = { x: chartRect.left + param.point.x, y: chartRect.top + param.point.y };
        }
        var row = null;
        for (var i = 0; i < candles.length; i++) if (candles[i].time === param.time) row = candles[i];
        if (row) {
          lastCrossPrice = num(row.close); /* M4: precarga del formulario con el crosshair */
          var d = new Date(row.time * 1000);
          $("st-ohlc").textContent = (chartType === "heikin" ? "OHLC real · " : "") + "O " + fmtP(row.open, market) + " · H " + fmtP(row.high, market) +
            " · L " + fmtP(row.low, market) + " · C " + fmtP(row.close, market) +
            " · " + d.toLocaleString("es-ES") + (lastPrice !== null ? " · último " + fmtP(lastPrice, market) : "");
        }
        /* Tooltip propio sobre las operaciones PAPER de esta vela. */
        updateMarkerTip(param.time);
      });
      try {
        chart.timeScale().subscribeVisibleLogicalRangeChange(function () {
          checkBackfill();
          guardPriceOnScroll();
          redrawDrawings();
        });
        chart.timeScale().subscribeVisibleTimeRangeChange(function (range) {
          if (!archiveRequested || !range) return;
          chartBox.dataset.archiveFrom = String(range.from);
          chartBox.dataset.archiveTo = String(range.to);
        });
      } catch (e) {}
      if (!chartBox._stDragBound) {
        chartBox._stDragBound = true;
        chartBox.addEventListener("click", onChartClick);
        chartBox.addEventListener("mousedown", onChartMouseDown);
        chartBox.addEventListener("mousemove", onChartMouseMove);
        chartBox.addEventListener("mouseup", onChartMouseUp);
        chartBox.addEventListener("mouseleave", onChartMouseLeave);
        chartBox.addEventListener("contextmenu", onContextMenu);
        /* Navegación nativa del motor (Fase 2): estos tres listeners son
         * memoria de escala (fijar/liberar), no pan/zoom duplicado. */
        chartBox.addEventListener("mousedown", onNavMouseDown);
        chartBox.addEventListener("mouseup", onNavMouseUp);
        chartBox.addEventListener("mouseleave", onNavMouseLeave);
        chartBox.addEventListener("dblclick", onNavDblClick);
      }
      applyCandleColors();
      return true;
    }

    /* --- Supresión de clic tras arrastre nativo + memoria de escala ----
     * El motor mueve con arrastre propio (pressedMouseMove): el navegador
     * igual dispara click al soltar, y sin este registro seleccionaría un
     * dibujo tras cada paneo. Solo mide distancia, no duplica el pan. */
    var navDown = null, suppressChartClick = false;
    function onNavMouseDown(ev) {
      if (ev.button !== undefined && ev.button !== 0) return;
      try {
        var r = chartBox.getBoundingClientRect();
        navDown = { x: ev.clientX - r.left, y: ev.clientY - r.top, price: snapshotPrice(), tag: priceProgTag };
      } catch (e) { navDown = null; }
    }
    function onNavMouseUp(ev) {
      if (!navDown) return;
      try {
        var r = chartBox.getBoundingClientRect();
        var dx = (ev.clientX - r.left) - navDown.x, dy = (ev.clientY - r.top) - navDown.y;
        if (Math.hypot(dx, dy) >= DRAG_MIN_PX) suppressChartClick = true;
      } catch (e) {}
      /* Escala manual: si el rango de precio cambió durante la pulsación
       * sin que Estudio lo ordenase (tag intacto) y el auto quedó
       * desactivado (el motor lo apaga al escalar en el eje), el usuario
       * fijó la escala: se respeta en pan/zoom/update hasta Centrar o
       * doble clic en el eje. */
      try {
        var cur = snapshotPrice();
        if (cur && cur.range && navDown.price && navDown.price.range &&
            priceProgTag === navDown.tag && !cur.auto &&
            JSON.stringify(cur.range) !== JSON.stringify(navDown.price.range)) {
          priceUserFixed = true;
          priceFrozenGap = false;
        }
      } catch (e) {}
      navDown = null;
    }
    function onNavMouseLeave() {
      navDown = null;
    }
    function onNavDblClick(ev) {
      /* Doble clic en el eje de precios: el motor ya restablece (nativo
       * axisDoubleClickReset); aquí se sincronizan los flags para que la
       * escala vuelva a automática de forma determinista. */
      try {
        var r = chartBox.getBoundingClientRect();
        var ps = priceScaleApi();
        var axW = 70;
        try { axW = (ps && ps.width ? ps.width() : 0) || 70; } catch (e2) {}
        if (ev.clientX - r.left >= (r.width || 0) - axW - 2) unlockPriceAuto();
      } catch (e) {}
    }

    /* --- Escala de precios fijada por el usuario + huecos sin datos ----
     * priceUserFixed: el usuario arrastró el eje (ver onNavMouseUp); no se
     * toca su rango en pan/zoom/repintado/update en vivo (el motor con
     * autoScale:false ya lo conserva: verificado en harness Node con 5
     * paneos + 3 zooms + update + setData + overscroll, todo estable).
     * priceFrozenGap: el paneo salió a una zona sin velas visibles; se
     * congela el último rango con datos (setVisibleRange apaga el auto
     * solo) en vez de colapsar la escala; al volver a datos se re-auto.
     * priceProgTag: cada cambio programático lo incrementa; onNavMouseUp
     * solo marca fijación manual si el tag no se movió (evita atribuir al
     * usuario un cambio del WS o del propio congelado). */
    var priceUserFixed = false, priceFrozenGap = false, priceProgTag = 0;
    var lastInDataPriceRange = null;
    function priceScaleApi() {
      try { return chart && chart.priceScale ? chart.priceScale("right") : null; } catch (e) { return null; }
    }
    function snapshotPrice() {
      try {
        var ps = priceScaleApi();
        if (!ps) return null;
        return { range: ps.getVisibleRange(), auto: !!(ps.options && ps.options().autoScale) };
      } catch (e) { return null; }
    }
    function unlockPriceAuto() {
      var ps = priceScaleApi();
      if (!ps) return;
      priceProgTag++;
      try { ps.applyOptions({ autoScale: true }); } catch (e) {}
      priceUserFixed = false;
      priceFrozenGap = false;
      /* Consistencia defensiva (Fase 3): al volver a auto no queda memoria
       * de rango con datos ni gesto de navegación a medias. */
      lastInDataPriceRange = null;
      navDown = null;
    }
    /* R1: memoria de escala por vista. Chart nuevo = escala automática de
     * nuevo: las 5 banderas vuelven a su valor inicial. Sin este reset,
     * tras aplicar un layout el gráfico nace con autoScale:true pero con
     * priceUserFixed=true heredado, y guardPriceOnScroll se salta su lógica.
     * Se llama en la ruta de creación de ensureChart (el early return de
     * arriba protege los repintados de paintAll, que reutilizan el chart) y
     * explícitamente desde loadAll/applyLayout, que destruyen el chart. */
    function resetScaleMemory() {
      priceUserFixed = false; priceFrozenGap = false; priceProgTag = 0; lastInDataPriceRange = null; navDown = null;
    }
    function guardPriceOnScroll() {
      if (!chart || !candles.length || priceUserFixed) return;
      var ps = priceScaleApi();
      if (!ps) return;
      var lr = null;
      try { lr = chart.timeScale().getVisibleLogicalRange(); } catch (e) { return; }
      if (!lr) return;
      var n = candles.length;
      var overlaps = lr.to >= 0 && lr.from <= n - 1;
      var snap = snapshotPrice();
      if (!snap || !snap.range) return;
      if (!overlaps) {
        /* Zona sin velas: congelar el último rango con datos. Sin esto la
         * escala colapsa a una rendija (medido en harness: {99,200} pasa a
         * {99,102} al ir a {-100,-50} con auto). */
        if (snap.auto && lastInDataPriceRange) {
          priceProgTag++;
          try { ps.setVisibleRange({ from: lastInDataPriceRange.from, to: lastInDataPriceRange.to }); } catch (e) {}
          priceFrozenGap = true;
        }
      } else if (snap.auto) {
        if (priceFrozenGap) {
          priceFrozenGap = false;
          unlockPriceAuto();
        } else {
          lastInDataPriceRange = { from: snap.range.from, to: snap.range.to };
        }
      }
    }

    function heikinAshiBars(rows) {
      var result = [], previousOpen = null, previousClose = null;
      (rows || []).forEach(function (row) {
        var open = num(row.open), high = num(row.high), low = num(row.low), close = num(row.close);
        var haClose = (open + high + low + close) / 4;
        var haOpen = previousOpen === null ? (open + close) / 2 : (previousOpen + previousClose) / 2;
        result.push({time:row.time, open:haOpen, high:Math.max(high, haOpen, haClose),
          low:Math.min(low, haOpen, haClose), close:haClose});
        previousOpen = haOpen;
        previousClose = haClose;
      });
      return result;
    }
    function toSeries() {
      if (chartType === "heikin") return heikinAshiBars(candles);
      return candles.map(function (c) {
        if (chartType === "velas" || chartType === "barras") {
          return { time: c.time, open: num(c.open), high: num(c.high), low: num(c.low), close: num(c.close) };
        }
        return { time: c.time, value: num(c.close) };
      });
    }
    function latestSeriesPoint() {
      if (!candles.length) return null;
      if (chartType === "heikin") {
        var bars = heikinAshiBars(candles);
        return bars[bars.length - 1];
      }
      var last = candles[candles.length - 1];
      return chartType === "velas" || chartType === "barras" ?
        {time:last.time, open:num(last.open), high:num(last.high), low:num(last.low), close:num(last.close)} :
        {time:last.time, value:num(last.close)};
    }
    function paintAll(keepRange, prependedBars) {
      if (!ensureChart()) return;
      var range = null;
      try { range = chart.timeScale().getVisibleLogicalRange(); } catch (e) {}
      priceSeries.setData(toSeries());
      applyPricePrecision();
      /* Volumen teñido con los bordes de vela (volRows): sigue a los
       * colores personalizados y legible en los 3 presets (B4). */
      volSeries.setData(volRows());
      paintIndicators();
      paintCompare();
      paintMarkers();
      refreshEntryPlan(); /* M3: plan de entrada + PnL (fetch con guarda; recarga, cambio y vela nueva) */
      if (keepRange && range) {
        /* Al anteponer historia, cada vela que ya estaba visible pasa a un
         * índice mayor. Restaurar los índices originales saltaría la vista
         * hacia la izquierda en lugar de conservar el mismo periodo. */
        var shift = Number(prependedBars) || 0;
        try { chart.timeScale().setVisibleLogicalRange({ from: range.from + shift, to: range.to + shift }); } catch (e) {}
      }
      redrawDrawings();
    }

    /* Caché de cálculo de indicadores (Fase 1, requisito de rendimiento):
     * paintIndicators() se llama en cada repintado; si ni los datos ni los
     * parámetros cambiaron, se reutilizan los arrays ya calculados en vez de
     * recalcular EMA/SMA/BB/RSI/MACD desde cero. La clave incluye longitud
     * del dataset, primera/última vela, último cierre, mercado,
     * temporalidad y parámetros: cualquier cambio de datos, TF, mercado o
     * parámetro invalida la caché.
     *
     * Reutilización de series (Fase 5.1, rendimiento + parpadeo): antes cada
     * repintado hacía removeSeries+addSeries de TODAS las líneas (medido en
     * stub Node con todo activo: 9 add + 9 rm por repintado, 90
     * recreaciones en 10 repintados). Ahora la serie se crea solo la
     * primera vez y se actualiza con applyOptions (color/título/grosor) +
     * setData (valores); solo se elimina la que deja de estar activa
     * (indicador apagado) o al destruir el chart. Verificado en el motor
     * real v5.0.9 (harness Node): applyOptions cambia color/título e
     * incluso priceScaleId sin recrear, setData actualiza, y
     * series.moveToPane() mueve de pane sin recrear. Qué obligaría a
     * recrear en LWC v5: NADA de lo que usan estos indicadores (todos los
     * cambios de configuración tienen vía de actualización); el pane se
     * fija en addSeries pero moveToPane lo cambia en vivo, así que ni un
     * cambio de pane exigiría recrear (camino defensivo con try/catch).
     * En la práctica el pane de cada clave nunca cambia (resto pane 0,
     * RSI pane 2, MACD pane 3), luego tras el primer pintado un repintado
     * es 0 add + 0 rm (medido: 9 set). La leyenda del motor ("EMA <p>")
     * y los valores son idénticos: misma caché indCacheKey, mismos rows. */
    var indCache = { key: null, vals: null };
    function paintIndicators() {
      if (!chart || !priceSeries) return;
      var LW = window.LightweightCharts;
      var cl = closes(candles);
      var paramsJson = JSON.stringify([indCfg.sma, indCfg.ema, indCfg.bb, indCfg.rsi, indCfg.macd]);
      var ckey = indCacheKey(candles.length,
        candles.length ? candles[0].time : -1,
        candles.length ? candles[candles.length - 1].time : -1,
        candles.length ? String(candles[candles.length - 1].close) : "",
        market, timeframe, paramsJson);
      var calc;
      if (indCache.key === ckey && indCache.vals) {
        calc = indCache.vals;
      } else {
        calc = { sma: {}, ema: {}, bb: null, rsi: {}, macd: null };
        if (indCfg.sma.on) calc.sma[num(indCfg.sma.p, 20)] = sma(cl, num(indCfg.sma.p, 20));
        if (indCfg.ema.on) {
          parsePeriodList(indCfg.ema.p, [20]).forEach(function (p) { calc.ema[p] = ema(cl, p); });
        }
        if (indCfg.bb.on) calc.bb = bollinger(cl, num(indCfg.bb.p, 20), num(indCfg.bb.m, 2));
        if (indCfg.rsi.on) calc.rsi[num(indCfg.rsi.p, 14)] = rsi(cl, num(indCfg.rsi.p, 14));
        if (indCfg.macd.on) calc.macd = macd(cl, num(indCfg.macd.f, 12), num(indCfg.macd.s, 26), num(indCfg.macd.sig, 9));
        indCache = { key: ckey, vals: calc };
      }
      /* Claves activas en este repintado; al final se elimina solo la serie
       * cuya clave ya no está activa (indicador apagado o periodo cambiado:
       * p. ej. pasar EMA de 20 a 50 retira "ema20" y crea "ema50"). */
      var wanted = {};
      /* No hay tooltip propio de indicadores en el Estudio (st-ohlc solo
       * muestra OHLC del crosshair): cada línea EMA se identifica con el
       * título de serie "EMA <periodo>" que el motor muestra en su leyenda. */
      function lineOf(vals, color, pane, key, title) {
        var rows = [];
        for (var i = 0; i < candles.length; i++) {
          if (vals[i] === null || vals[i] === undefined) continue;
          rows.push({ time: candles[i].time, value: vals[i], color: color });
        }
        var sk = key || (color + (pane || 0));
        if (!rows.length) {
          if (indSeries[sk]) {
            try { chart.removeSeries(indSeries[sk]); } catch (e) {}
            delete indSeries[sk]; delete indSeriesMeta[sk];
          }
          return;
        }
        wanted[sk] = true;
        var opts = { color: color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false };
        if (title) opts.title = title;
        var prev = indSeries[sk];
        var pnum = pane || 0;
        if (prev) {
          var meta = indSeriesMeta[sk] || {};
          if (meta.pane !== undefined && meta.pane !== pnum) {
            /* No ocurre con las claves actuales (pane fijo por tipo), pero
             * si alguna clave cambiase de pane se mueve en vivo; solo si el
             * motor no lo soporta se recrea (defensivo). */
            var moved = false;
            try { if (prev.moveToPane) { prev.moveToPane(pnum); moved = true; } } catch (e) { moved = false; }
            if (!moved) {
              try { chart.removeSeries(prev); } catch (e) {}
              prev = null;
            }
          }
        }
        if (prev) {
          try { prev.applyOptions(opts); } catch (e) {}
          try { prev.setData(rows); } catch (e) {}
          indSeriesMeta[sk] = { pane: pnum };
          return;
        }
        var s = chart.addSeries(LW.LineSeries, opts, pnum);
        s.setData(rows);
        indSeries[sk] = s;
        indSeriesMeta[sk] = { pane: pnum };
      }
      if (indCfg.sma.on) lineOf(calc.sma[num(indCfg.sma.p, 20)], "#e3b341", 0, "sma" + num(indCfg.sma.p, 20), "SMA " + num(indCfg.sma.p, 20));
      if (indCfg.ema.on) {
        parsePeriodList(indCfg.ema.p, [20]).forEach(function (p) {
          if (calc.ema[p]) lineOf(calc.ema[p], emaColorFor(p), 0, "ema" + p, "EMA " + p);
        });
      }
      if (indCfg.bb.on && calc.bb) {
        var bb = calc.bb;
        /* Clave con parámetros (defensa en profundidad, igual que SMA/EMA/
         * RSI): la caché indCacheKey ya invalida al cambiar p/m, pero si la
         * clave fuese fija un fallo de caché reutilizaría datos erróneos.
         * Con parámetros en la clave, cambiar p/m retira las 3 viejas y
         * crea las 3 nuevas (mismo barrido que un periodo EMA); en
         * repintados normales la clave es estable: 0 add + 0 rm. */
        var bbp = num(indCfg.bb.p, 20), bbm = num(indCfg.bb.m, 2);
        lineOf(bb.up, "#3fb950", 0, "bbup" + bbp + "m" + bbm, "BB sup"); lineOf(bb.mid, "#9aa7b8", 0, "bbmid" + bbp + "m" + bbm, "BB med"); lineOf(bb.lo, "#f85149", 0, "bblo" + bbp + "m" + bbm, "BB inf");
      }
      if (indCfg.rsi.on) lineOf(calc.rsi[num(indCfg.rsi.p, 14)], "#d29922", 2, "rsi" + num(indCfg.rsi.p, 14), "RSI " + num(indCfg.rsi.p, 14));
      if (indCfg.macd.on && calc.macd) {
        var m = calc.macd;
        /* Idem BB: la clave lleva f/s/sig (ver comentario de arriba). */
        var mf = num(indCfg.macd.f, 12), ms = num(indCfg.macd.s, 26), mg = num(indCfg.macd.sig, 9);
        lineOf(m.line, "#4da3ff", 3, "macdline" + mf + "_" + ms + "_" + mg, "MACD"); lineOf(m.signal, "#f85149", 3, "macdsig" + mf + "_" + ms + "_" + mg, "MACD señal");
      }
      /* Barrido: solo se elimina la serie cuya clave dejó de estar activa
       * (indicador apagado o periodo que ya no se pide). Todo lo demás se
       * reutilizó arriba: 0 add + 0 rm en el repintado idéntico. */
      Object.keys(indSeries).forEach(function (k) {
        if (!wanted[k]) {
          try { chart.removeSeries(indSeries[k]); } catch (e) {}
          delete indSeries[k]; delete indSeriesMeta[k];
        }
      });
    }
    /* Si el periodo máximo activo necesita más velas de las cargadas, se
     * recargan con el limit adecuado (p. ej. activar EMA-200 con 500 velas
     * pide 800). Solo crece: nunca recorta el dataset ya cargado. */
    function maybeRefillForIndicators() {
      if (loading) return;
      if (candles.length >= candleLimitFor(indCfg)) return;
      loadCandles(false);
    }

    function paintCompare() {
      var LW = window.LightweightCharts;
      if (cmpSeries) { try { chart.removeSeries(cmpSeries); } catch (e) {} cmpSeries = null; }
      if (!compareSymbol || !compareSymbol.rows || !compareSymbol.rows.length || !candles.length) return;
      var base0 = num(candles[0].close), c0 = num(compareSymbol.rows[0].c);
      if (!base0 || !c0) return;
      var byT = {};
      compareSymbol.rows.forEach(function (r) { byT[r.t] = num(r.c); });
      var rows = [];
      /* Tanda C4: las velas del Estudio viajan MAPEADAS ({time,...} vía
       * mapCandleRow) mientras la comparada viaja CRUDA ({t,...}): emparejar
       * por c.time (antes c.t, siempre undefined → rows vacío → nunca
       * pintaba). Mismo formato de tiempo que el resto (épocas s). */
      candles.forEach(function (c) {
        if (byT[c.time] === undefined) return;
        rows.push({ time: c.time, value: (byT[c.time] / c0 - 1) * 100 });
      });
      if (rows.length < 2) return;
      /* La comparada es una línea de % normalizada con semántica propia
       * (diferencia vs base), no velas: se deja el dorado #e3b341 para no
       * confundirla con alcista/bajista (B4: no procede teñirla). */
      cmpSeries = chart.addSeries(LW.LineSeries, { color: "#e3b341", lineWidth: 1, priceLineVisible: false, lastValueVisible: true });
      cmpSeries.setData(rows);
    }

    /* Precisión adaptativa del eje de precios (Fase 0, motor real): el motor
     * usa por defecto precision:2/minMove:.01, ilegible en activos de precio
     * muy pequeño (0.00000123) o con muchos decimales. Se calcula desde los
     * datos cargados: decimales para que el máximo supere ~1000 unidades del
     * último decimal, entre 2 y 8. BTC (~1e5) -> 2; altcoin 1.2e-6 -> 8. */
    function priceDecimalsFor(rows) {
      var mx = 0;
      for (var i = 0; i < rows.length; i++) {
        var v = Math.abs(num(rows[i].close));
        if (v > mx) mx = v;
      }
      if (!(mx > 0)) return 2;
      var p = Math.ceil(3 - Math.log10(mx));
      if (p < 2) p = 2;
      if (p > 8) p = 8;
      return p;
    }
    function applyPricePrecision() {
      if (!priceSeries || !candles.length) return;
      var p = priceDecimalsFor(candles);
      try { priceSeries.applyOptions({ priceFormat: { type: "price", precision: p, minMove: 1 / Math.pow(10, p) } }); } catch (e) {}
    }

    function priceMarkersApi() {
      if (!priceMarkers && priceSeries && window.LightweightCharts) {
        try { priceMarkers = window.LightweightCharts.createSeriesMarkers(priceSeries, []); } catch (e) { priceMarkers = null; }
      }
      return priceMarkers;
    }
    function refreshMarkers() {
      var api = priceMarkersApi();
      if (!api) return;
      try { api.setMarkers(paperMarks.concat(planMarks)); } catch (e) {}
    }

    /* M1: operaciones PAPER en memoria + marcadores legibles + tooltip.
     * El motor v5.0.9 NO renderiza el campo `title` de los marcadores
     * (barrido hover exhaustivo: 0 tooltips), así que `title` deja de usarse
     * como portador de información: la tarjeta #st-marker-tip muestra fecha,
     * lado, cantidad, precio y comisión desde paperOps (datos crudos de
     * /paper/operations). Los marcadores usan círculo (compra) / cuadrado
     * (venta) con size 2; si coinciden varias operaciones en una vela,
     * muestran su recuento por lado, no una cantidad inventada. Para una
     * compra sola, el texto sigue siendo "P · 0,01 BTC";
     * legibles sin zoom (las flechas medían ~2 px con 500 velas). */
    function opEpoch(o) {
      try {
        var raw = String((o && o.timestamp) || "").trim().replace(" ", "T");
        var t = Math.floor(Date.parse(/[Zz]$|[+-]\d{2}:\d{2}$/.test(raw) ? raw : raw + "Z") / 1000);
        return isFinite(t) ? t : null;
      } catch (e) { return null; }
    }
    function paperBarTime(epoch, bars, seconds, monthly) {
      if (!Array.isArray(bars) || !bars.length || !isFinite(epoch)) return null;
      var lo = 0, hi = bars.length;
      while (lo < hi) {
        var mid = Math.floor((lo + hi) / 2);
        if (Number(bars[mid].time) <= epoch) lo = mid + 1;
        else hi = mid;
      }
      if (!lo) return null;
      var start = Number(bars[lo - 1].time);
      if (!isFinite(start)) return null;
      var end = candleEndTime(start, seconds, monthly);
      if (lo < bars.length) end = Math.min(end, Number(bars[lo].time));
      return isFinite(end) && epoch < end ? start : null;
    }
    function paperChartBounds(bars, seconds, monthly) {
      if (!Array.isArray(bars) || !bars.length || !(seconds > 0)) return null;
      var first = Number(bars[0].time), last = Number(bars[bars.length - 1].time);
      if (!isFinite(first) || !isFinite(last) || first > last) return null;
      var end = candleEndTime(last, seconds, monthly);
      if (!isFinite(end) || end <= first) return null;
      return { start: new Date(first * 1000).toISOString(), end: new Date(end * 1000).toISOString() };
    }
    function paperQtyText(o) {
      var units = o.operation_type === "SELL" ? o.amount_spent : o.asset_received;
      try { return U.fmtQty(units, o.asset); } catch (e) { return String(units); }
    }
    function paperFee(o) {
      return (num(o.trading_fee) || 0) + (num(o.withdrawal_fee) || 0) + (num(o.network_fee) || 0);
    }
    function groupPaperOps(ops, mk, bars, seconds, monthly) {
      var groups = Object.create(null);
      (ops || []).forEach(function (o) {
        if (!o || (o.asset + "/" + o.quote_currency) !== mk ||
            (o.operation_type !== "BUY" && o.operation_type !== "SELL")) return;
        var t = opEpoch(o);
        if (t === null) return;
        if (bars) {
          t = paperBarTime(t, bars, seconds, monthly);
          if (t === null) return;
        }
        if (!groups[t]) groups[t] = { time: t, buy: [], sell: [] };
        groups[t][o.operation_type === "BUY" ? "buy" : "sell"].push(o);
      });
      return groups;
    }
    /* Una marca por lado y vela; el número impide que varias operaciones
     * superpuestas parezcan una sola. La tarjeta conserva las individuales. */
    function buildPaperMarks(ops, mk, bars, seconds, monthly, grouped) {
      var groups = grouped || groupPaperOps(ops, mk, bars, seconds, monthly);
      var out = [];
      Object.keys(groups).sort(function (a, b) { return Number(a) - Number(b); }).forEach(function (key) {
        var group = groups[key];
        if (group.buy.length) out.push({
          time: group.time, position: "belowBar", shape: "circle", color: "#3fb950", size: 2,
          text: group.buy.length === 1 ? ("P · " + paperQtyText(group.buy[0])) : ("P ×" + group.buy.length)
        });
        if (group.sell.length) out.push({
          time: group.time, position: "aboveBar", shape: "square", color: "#f85149", size: 2,
          text: group.sell.length === 1 ? "S" : ("S ×" + group.sell.length)
        });
      });
      return out;
    }
    /* Tarjeta del tooltip (HTML escapado; mismos formateadores que
     * Operativa: fecha U.fmtDateTime, moneda "74.195,10 €" vía U.fmtMoney,
     * cantidad vía U.fmtQty). Pura salvo U: testeable en Node. */
    function paperTipHtml(o) {
      var isBuy = o.operation_type === "BUY";
      var quote = o.quote_currency;
      var quoteAmount = isBuy ? o.amount_spent : o.asset_received;
      return '<strong class="mt-side ' + (isBuy ? "buy" : "sell") + '">' +
        U.esc(isBuy ? "Compra PAPER" : "Venta PAPER") + "</strong>" +
        '<span class="mt-row">' + U.esc(U.fmtDateTime(o.timestamp)) + "</span>" +
        '<span class="mt-row">Cantidad ' + U.esc(paperQtyText(o)) + "</span>" +
        '<span class="mt-row">Precio ' + U.esc(U.fmtMoney(o.price, quote)) + "</span>" +
        '<span class="mt-row">Importe ' + U.esc(U.fmtMoney(quoteAmount, quote)) + "</span>" +
        '<span class="mt-row">Comisión ' + U.esc(U.fmtMoney(String(paperFee(o)), quote)) + "</span>";
    }
    function paperTipGroupHtml(group) {
      if (!group) return "";
      var ops = group.buy.concat(group.sell).sort(function (a, b) {
        var diff = opEpoch(a) - opEpoch(b);
        return diff || Number(a.id || 0) - Number(b.id || 0);
      });
      if (ops.length === 1) return paperTipHtml(ops[0]);
      var shown = ops.slice(0, 2).map(function (op) {
        return '<div class="mt-operation">' + paperTipHtml(op) + "</div>";
      }).join("");
      return '<strong class="mt-count">' + ops.length + ' operaciones PAPER en esta vela</strong>' +
        shown + (ops.length > 2 ? '<span class="mt-row">' + (ops.length - 2) +
        ' más en Movimientos</span>' : "");
    }
    /* Compatibilidad con los antiguos consumidores/tests M1 y C1. La ficha
     * visible usa paperBarGroups y nunca selecciona una sola operación de
     * una vela que contiene varias. */
    function paperOpNear(t, maxDt) {
      var best = null, bestDt = Infinity;
      var matchBar = maxDt !== undefined && maxDt !== null &&
        typeof candles !== "undefined" && Array.isArray(candles) && candles.length > 0;
      for (var i = 0; i < paperOps.length; i++) {
        var e = opEpoch(paperOps[i]);
        if (e === null) continue;
        if (matchBar) {
          if (paperBarTime(e, candles, tfSeconds(timeframe), timeframe === "1M") !== t) continue;
        }
        var dt = Math.abs(e - t);
        if (dt < bestDt) { bestDt = dt; best = paperOps[i]; }
      }
      if (best === null) return null;
      var lim = Number(maxDt);
      if (!matchBar && maxDt !== undefined && maxDt !== null && isFinite(lim) && bestDt > lim) return null;
      return best;
    }
    /* M3: decisión pura de líneas de entrada + PnL (testeable en Node).
     * Dado el `positions` crudo de GET /paper/portfolio y el mercado
     * visible, devuelve el plan { market, averageCost, pnlPct, pnlAbs } o
     * null si no hay que pintar nada (casilla PAPER apagada, modo LIVE,
     * sin posición abierta en este mercado o coste medio no numérico).
     * El % sale del campo `return_percentage` del servidor (NO existe
     * `unrealized_pnl_pct`); si falta o no es numérico se calcula en
     * presentación desde `unrealized_pnl`/`invested` del propio payload
     * (aceptable: mismos campos del servidor, nunca inventado); en último
     * término 0 (la etiqueta viva la recalcula livePnlPct con el precio). */
    function entryLinePlan(positions, mk, showPaper, isPaper) {
      if (!showPaper || !isPaper) return null;
      var list = positions || [];
      for (var i = 0; i < list.length; i++) {
        var p = list[i];
        if (!p) continue;
        if ((p.asset + "/" + p.quote_currency) !== mk) continue;
        var avg = Number(p.average_cost);
        if (!isFinite(avg) || avg <= 0) return null;
        var pct = (p.return_percentage !== null && p.return_percentage !== undefined)
          ? Number(p.return_percentage) : NaN;
        if (!isFinite(pct)) {
          var u = Number(p.unrealized_pnl), inv = Number(p.invested);
          pct = (isFinite(u) && isFinite(inv) && inv > 0) ? (u / inv * 100) : 0;
        }
        var abs = Number(p.unrealized_pnl);
        return { market: mk, averageCost: avg, pnlPct: pct, pnlAbs: isFinite(abs) ? abs : 0 };
      }
      return null;
    }
    /* M3: % vivo desde el coste medio (servidor) y el precio del gráfico.
     * La etiqueta cambia con cada tick sin pedir el portfolio; null si no
     * hay precio publicable. Pura y testeable en Node. */
    function livePnlPct(avg, price) {
      /* Number(null) es 0 y Number("") es 0: los nulos se rechazan antes
       * para no pintar un -100 % inventado sin precio publicable. */
      if (avg === null || avg === undefined || avg === "" ||
          price === null || price === undefined || price === "") return null;
      var a = Number(avg), px = Number(price);
      if (!isFinite(a) || a <= 0 || !isFinite(px)) return null;
      return (px - a) / a * 100;
    }
    /* M3: etiqueta de la línea de PnL con el formateo es-ES EXISTENTE
     * (Tanda C6): el canvas no localiza y toFixed(2) pintaba "-0.00%" con
     * punto; ahora viaja YA formateada ("-0,00%") con las reglas de U.fmtPct
     * en app.js (Intl es-ES, 2 decimales, signo). Solo difiere de fmtPct en
     * el cero exacto: signo "always" para conservar el "+" de M3 ("+0,00%",
     * como exigía test_m3_entry_pnl) en vez del "0,00%" de exceptZero; el
     * ejemplo del encargo ("-0,00%") también exige conservar el signo tras
     * redondeo. Intl existe en los navegadores soportados y en Node; sin
     * Intl, reserva con punto (nunca "—" por un formateador ausente). Pura
     * y testeable en Node.
     * Equivalencia M3 (test_m3_entry_pnl.py): mismos casos, coma es-ES. */
    function pnlLabel(pct) {
      var n = Number(pct);
      if (!isFinite(n)) return "—";
      try {
        return new Intl.NumberFormat("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "always" }).format(n) + "%";
      } catch (e) {
        return (n < 0 ? "-" : "+") + Math.abs(n).toFixed(2) + "%";
      }
    }
    /* M3: color de la línea de PnL (mismos verde/rojo que los orbes
     * upDownMarkers de Operativa). Puro y testeable en Node. */
    function pnlColor(pct) {
      return (Number(pct) >= 0) ? "#22AB94" : "#F7525F";
    }
    function hideMarkerTip() {
      var tip = $("st-marker-tip");
      if (tip) tip.hidden = true;
    }
    function setPaperMarksStatus(message) {
      var status = $("st-paper-marks-status");
      if (!status) return;
      status.textContent = message || "";
      status.hidden = !message;
    }
    /* Pinta la tarjeta junto al cursor (crosshair con tiempo y punto).
     * Posición con getBoundingClientRect (mismo patrón que pointFromEvent)
     * y clamp dentro del contenedor del gráfico para que nunca se salga.
     * Respeta la casilla PAPER: apagada, ni marcadores ni tooltip. */
    function updateMarkerTip(t) {
      var tip = $("st-marker-tip");
      if (!tip) return;
      if (!showPAPER || !paperOps.length || t === undefined || t === null) { tip.hidden = true; return; }
      var group = paperBarGroups[t];
      if (!group || !lastMouse) { tip.hidden = true; return; }
      tip.innerHTML = paperTipGroupHtml(group);
      tip.hidden = false;
      try {
        var wrap = $("st-wrap"), rW = wrap.getBoundingClientRect(), rC = chartBox.getBoundingClientRect();
        var x = lastMouse.x - rW.left + 14, y = lastMouse.y - rW.top + 14;
        var tw = tip.offsetWidth || 220, th = tip.offsetHeight || 130;
        var maxX = (rW.width || rC.width || 600) - tw - 8;
        var maxY = (rC.height || 380) - (rC.top - rW.top) - th - 8;
        if (x > maxX) x = Math.max(8, maxX);
        if (y > maxY) y = Math.max(8, maxY);
        if (x < 8) x = 8;
        if (y < 8) y = 8;
        tip.style.left = Math.round(x) + "px";
        tip.style.top = Math.round(y) + "px";
      } catch (e) {}
    }

    function paintMarkers() {
      if (!chart || !priceSeries || !window.LightweightCharts) return;
      /* Guarda anti-carrera: si el usuario cambió de mercado mientras la
       * petición volaba, la respuesta tardía no debe sustituir (setMarkers
       * REEMPLAZA) los marcadores del mercado actual. */
      var mk = market;
      var chartRef = chart;
      var markerSeq = ++paperMarkerRequestSeq;
      if (!showPAPER) {
        /* Casilla PAPER apagada: ni se pide ni se pinta (tampoco tooltip).
         * paperOps se vacía para que updateMarkerTip no tenga qué mostrar. */
        paperOps = []; paperMarks = []; paperBarGroups = Object.create(null);
        refreshMarkers(); hideMarkerTip(); setPaperMarksStatus("");
      } else {
        var bounds = paperChartBounds(candles, tfSeconds(timeframe), timeframe === "1M");
        if (!bounds) {
          paperOps = []; paperMarks = []; paperBarGroups = Object.create(null);
          refreshMarkers(); hideMarkerTip(); setPaperMarksStatus("");
        } else {
          var pair = mk.split("/");
          var query = "/paper/operations?asset=" + encodeURIComponent(pair[0]) +
            "&quote=" + encodeURIComponent(pair[1]) +
            "&start_at=" + encodeURIComponent(bounds.start) +
            "&end_before=" + encodeURIComponent(bounds.end);
          setPaperMarksStatus("");
          U.api(query).then(function (r) {
            if (markerSeq !== paperMarkerRequestSeq || mk !== market || chartRef !== chart || !showPAPER) return;
            if (!r.ok || !Array.isArray(r.data)) {
              paperOps = []; paperMarks = []; paperBarGroups = Object.create(null);
              refreshMarkers(); hideMarkerTip();
              setPaperMarksStatus("Marcas PAPER no disponibles. Reintenta actualizar el gráfico.");
              return;
            }
            paperOps = r.data.filter(function (o) {
              return o && (o.asset + "/" + o.quote_currency) === market;
            });
            paperBarGroups = groupPaperOps(paperOps, mk, candles, tfSeconds(timeframe), timeframe === "1M");
            paperMarks = buildPaperMarks(paperOps, mk, candles, tfSeconds(timeframe), timeframe === "1M", paperBarGroups);
            refreshMarkers();
            hideMarkerTip();
          }).catch(function () {
            if (markerSeq !== paperMarkerRequestSeq || mk !== market || chartRef !== chart || !showPAPER) return;
            paperOps = []; paperMarks = []; paperBarGroups = Object.create(null);
            refreshMarkers(); hideMarkerTip();
            setPaperMarksStatus("Marcas PAPER no disponibles. Reintenta actualizar el gráfico.");
          });
        }
      }
      if (showDCA || showPlans) {
        U.api("/strategies").then(function (r) {
          if (mk !== market) return;
          var extra = [];
          if (r.ok && r.data && r.data.length) {
            r.data.forEach(function (s) {
              var cfg = s.config || {};
              if ((cfg.asset || cfg.base || "") + "/" + (cfg.quote_currency || cfg.quote || "") !== market) return;
              if (candles.length) extra.push({ time: candles[candles.length - 1].time, position: "aboveBar", shape: "circle", color: "#7c5cff", size: 2, text: "D" });
            });
          }
          planMarks = extra;
          refreshMarkers();
        }).catch(function () {});
      } else {
        planMarks = [];
        refreshMarkers();
      }
    }

    /* M3: líneas de entrada + PnL flotante (estilo Binance/Bitget).
     * - Línea discontinua ámbar en el coste medio (etiqueta "Entrada …",
     *   INMÓVIL: se crea una vez por plan y nunca se le toca el precio).
     * - Línea en el precio vivo cuya etiqueta muestra el PnL no realizado
     *   en % (verde/rojo): se mueve con cada tick vía applyOptions sobre
     *   la MISMA instancia (verificado en el vendor v5.0.9: title y price
     *   actualizables sin recrear, harness M3).
     * Sin posición abierta no hay línea de entrada. No fija scaleMargins
     * ni toca guardPriceOnScroll/priceUserFixed/autoscaleInfoProvider: las
     * price lines no pelean con la escala (el usuario sigue fijándola con
     * el eje y Centrar la libera). */
    function clearEntryLines() {
      if (priceSeries && entryLine) { try { priceSeries.removePriceLine(entryLine); } catch (e) {} }
      if (priceSeries && pnlLine) { try { priceSeries.removePriceLine(pnlLine); } catch (e) {} }
      entryLine = null; pnlLine = null;
    }
    function paintEntryLines() {
      if (!chart || !priceSeries || !window.LightweightCharts) return;
      if (!showPAPER || U.orbisMode() !== "PAPER" || !entryPlan || entryPlan.market !== market) {
        /* Casilla apagada, LIVE o plan de otro mercado: retirar sin pedir. */
        clearEntryLines();
        return;
      }
      if (!entryLine) {
        try {
          entryLine = priceSeries.createPriceLine({
            price: entryPlan.averageCost, color: "#e3b341", lineWidth: 1,
            lineStyle: 2, lineVisible: true, axisLabelVisible: true,
            title: "Entrada " + fmtP(entryPlan.averageCost, market)
          });
        } catch (e) { entryLine = null; }
      }
      /* La de entrada NO se mueve: solo la viva sigue al precio.
       * num(null) es 0: sin precio publicable no hay línea viva (nunca un
       * -100 % inventado). */
      if (lastPrice === null || lastPrice === undefined || lastPrice === "") {
        if (pnlLine) { try { priceSeries.removePriceLine(pnlLine); } catch (e) {} pnlLine = null; }
        return;
      }
      var px = num(lastPrice);
      if (!isFinite(px)) {
        if (pnlLine) { try { priceSeries.removePriceLine(pnlLine); } catch (e) {} pnlLine = null; }
        return;
      }
      var pct = livePnlPct(entryPlan.averageCost, px);
      if (pct === null) {
        if (pnlLine) { try { priceSeries.removePriceLine(pnlLine); } catch (e) {} pnlLine = null; }
        return;
      }
      var color = pnlColor(pct), title = pnlLabel(pct);
      if (!pnlLine) {
        try {
          pnlLine = priceSeries.createPriceLine({
            price: px, color: color, lineWidth: 1,
            lineStyle: 0, lineVisible: true, axisLabelVisible: true, title: title
          });
        } catch (e) { pnlLine = null; }
      } else {
        try { pnlLine.applyOptions({ price: px, color: color, title: title }); } catch (e) {}
      }
    }
    /* M3: trae el plan desde GET /paper/portfolio (quote del mercado
     * visible) con guarda anti-carrera como paintMarkers: si el usuario
     * cambió de mercado mientras volaba, la respuesta tardía se ignora.
     * Sin escenario/posición se pide y se ignora sin romper (plan null =
     * sin líneas). No se pide con la casilla apagada ni en LIVE. */
    function refreshEntryPlan() {
      if (!showPAPER || U.orbisMode() !== "PAPER") { entryPlan = null; paintEntryLines(); return; }
      var mk = market, parts = tradeParts();
      U.api("/paper/portfolio?quote_currency=" + encodeURIComponent(parts.quote)).then(function (r) {
        if (mk !== market) return;
        var positions = (r.ok && r.data && Array.isArray(r.data.positions)) ? r.data.positions : [];
        entryPlan = entryLinePlan(positions, mk, showPAPER, U.orbisMode() === "PAPER");
        paintEntryLines();
      }).catch(function () { /* sin portfolio no se inventa nada: se conserva el plan anterior */ });
    }

    /* M4: operar PAPER desde el gráfico (cierra B1-3, reutiliza M1).
     * El formulario mínimo (cantidad + cotización actual del par visible)
     * envía por el helper
     * compartido U.postPaperTrade —el MISMO constructor de body que
     * Operativa (request_id nuevo por clic, nunca strategy_id)— y tras
     * 201/200 con executed:true refresca paintMarkers() + loadSnapshot()
     * sin recargar. Los bloqueos Guard se muestran honestos en el formulario
     * con las mismas frases de Operativa (U.guardPhrase) y no crean marca
     * nueva. Sin precio publicado no se opera. Opera siempre el mercado
     * visible (con su quote). Solo PAPER: en LIVE se bloquea. */
    function tradeParts() {
      var p = String(market || "BTC/EUR").split("/");
      return { asset: p[0] || "BTC", quote: p[1] || "EUR" };
    }
    /* Pura y testeable: rechaza precio histórico, otra pareja, reloj
     * incoherente, cotización vieja o marca del proveedor atrasada. */
    function tradeSnapshotPrice(quote, visibleMarket, wallNowMs, elapsedMs) {
      if (!quote || !quote.available || quote.symbol !== visibleMarket ||
          ["stale", "future", "invalid"].indexOf(quote.market_time_status) >= 0) return null;
      var price = Number(quote.price);
      var wallAgeMs = wallNowMs - Date.parse(quote.received_at);
      if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(wallAgeMs) ||
          wallAgeMs < -120000 || wallAgeMs > 60000 ||
          !Number.isFinite(elapsedMs) || elapsedMs < 0 || elapsedMs > 60000) return null;
      return price;
    }
    function currentTradePrice() {
      return tradeSnapshotPrice(lastTradeQuote, market, Date.now(),
        lastTradeQuoteReceivedMs === null ? NaN : performance.now() - lastTradeQuoteReceivedMs);
    }
    function syncTradePrice() {
      var input = $("st-trade-price");
      if (input) {
        var px = currentTradePrice();
        input.value = px === null ? "" : String(px);
      }
    }
    function paintTradePair() {
      var el = $("st-trade-pair");
      if (!el) return;
      var tradePrice = currentTradePrice();
      el.textContent = market + " · " +
        (tradePrice !== null ? "cotización " + fmtP(tradePrice, market) : "cotización actual no disponible");
    }
    function openTradeForm(side) {
      var parts = tradeParts();
      tradeSide = (side === "sell") ? "sell" : "buy";
      var form = $("st-trade-form"), msg = $("st-trade-msg");
      if (msg) msg.innerHTML = "";
      if (!form) return;
      if (provider === "grvt" || market.endsWith(":PERP")) {
        form.hidden = true;
        if (msg) msg.textContent = "GRVT PERP: operativa PAPER LONG/SHORT pendiente; este formulario sólo registra spot.";
        return;
      }
      var px = currentTradePrice();
      var sideEl = $("st-trade-side"), qtyLab = $("st-trade-qtylab");
      if (sideEl) sideEl.textContent = (tradeSide === "sell" ? "Vender " : "Comprar ") + market + " · PAPER (dinero simulado)";
      if (qtyLab) qtyLab.textContent = (tradeSide === "sell" ? "Cantidad " + parts.asset : "Importe " + parts.quote);
      $("st-trade-qty").value = (tradeSide === "sell") ? "0.001" : "100";
      $("st-trade-price").value = (px === null) ? "" : String(px);
      form.hidden = false;
      try { $("st-trade-qty").focus(); } catch (e) {}
      /* M4: al vender se muestra lo disponible (misma fuente que Operativa:
       * /paper/portfolio) y se usa en la frase Guard; el servidor valida. */
      lastAvailQty = null;
      if (tradeSide === "sell") {
        (function (mk, a, q) {
          U.api("/paper/portfolio?quote_currency=" + encodeURIComponent(q)).then(function (r) {
            if (mk !== market || tradeSide !== "sell") return;
            if (!(r.ok && r.data && Array.isArray(r.data.positions))) return;
            for (var i = 0; i < r.data.positions.length; i++) {
              if (r.data.positions[i].asset === a) {
                lastAvailQty = r.data.positions[i].quantity;
                var sEl = $("st-trade-side"), txt = "";
                try { txt = U.fmtQty(lastAvailQty, a); } catch (e) { txt = String(lastAvailQty); }
                if (sEl) sEl.textContent += " · disponible " + txt;
              }
            }
          }).catch(function () {});
        })(market, parts.asset, parts.quote);
      }
    }
    function closeTradeForm() {
      tradeSide = null;
      lastAvailQty = null;
      var form = $("st-trade-form");
      if (form) form.hidden = true;
    }
    function tradeMsg(html) {
      var msg = $("st-trade-msg");
      if (msg) msg.innerHTML = html;
    }
    function armTradeRetry() {
      var msg = $("st-trade-msg");
      /* Ante una respuesta incierta, reenvía la misma intención con el mismo
       * request_id. El servidor confirma o rechaza ese ID sin duplicarla. */
      try { U.armAnyRetry(msg, function () { var f = $("st-trade-form"); if (f && f.requestSubmit) f.requestSubmit(); }); } catch (e) {}
    }
    function submitTradeForm(ev) {
      ev.preventDefault();
      if (provider === "grvt" || market.endsWith(":PERP")) {
        tradeMsg('<div class="amber" role="status">GRVT PERP aún no admite operaciones PAPER.</div>');
        return;
      }
      var btn = $("st-trade-send");
      if (!btn || btn.disabled) return; /* anti doble-clic: 1 envío = 1 operación */
      var parts = tradeParts();
      var mkAtSend = market;
      if (U.orbisMode() !== "PAPER") {
        tradeMsg('<div class="amber" role="status"><strong>La operativa simulada está en PAPER.</strong> Vuelve a PAPER para operar.</div>');
        try { U.showToast("blocked", "Vuelve a PAPER para operar."); } catch (e) {}
        return;
      }
      if (!U.postMarketPaperTrade) {
        tradeMsg('<div class="state error" role="alert"><strong>No se puede operar todavía.</strong> Recarga la página para actualizar la aplicación.</div>');
        return;
      }
      var currentPrice = currentTradePrice();
      if (currentPrice === null) {
        syncTradePrice();
        tradeMsg('<div class="state empty" role="status"><strong>Sin cotización actual del mercado visible.</strong> Actualiza el precio antes de operar.</div>');
        try { U.showToast("blocked", "Sin cotización actual no se puede operar."); } catch (e) {}
        return;
      }
      var qty = String(($("st-trade-qty").value || "").trim()).replace(",", ".");
      syncTradePrice();
      if (!qty || !(Number(qty) > 0)) {
        tradeMsg('<div class="amber" role="status"><strong>Revisa la cantidad:</strong> tiene que ser mayor que 0.</div>');
        return;
      }
      btn.disabled = true;
      tradeMsg('<div class="state loading" role="status">Enviando ' + U.esc(tradeSide === "sell" ? "venta" : "compra") + "…</div>");
      var fields = { asset: parts.asset, quote_currency: parts.quote,
        provider: lastResolvedProvider || provider };
      if (tradeSide === "sell") { fields.quantity = qty; fields.fee = "0"; }
      else { fields.order_amount = qty; fields.estimated_fee = "0"; }
      var intentKey = JSON.stringify([tradeSide, market, qty, fields.provider]);
      if (!pendingTradeRequest || pendingTradeRequest.key !== intentKey) {
        pendingTradeRequest = { key: intentKey, id: U.newRequestId() };
      }
      fields.request_id = pendingTradeRequest.id;
      U.postMarketPaperTrade(tradeSide === "sell" ? "sell" : "buy", fields).then(function (res) {
        /* Guarda anti-carrera (igual que paintMarkers): si el usuario cambió
         * de mercado mientras volaba, no se pinta nada ajeno. */
        if (mkAtSend !== market || !$("st-trade-msg") || !chartBox.isConnected) { btn.disabled = false; return; }
        btn.disabled = false;
        if (res.ok || res.status === 409 || res.status === 422) pendingTradeRequest = null;
        if (!res.ok) {
          var retryable = res.status >= 500 ||
            !!(res.data && res.data.code === "market_quote_unavailable");
          var errorHtml = U.httpErrorBox(res, { quote: parts.quote });
          if (retryable && errorHtml.indexOf("data-retry=") < 0)
            errorHtml += ' <button type="button" class="ghost" data-retry="st-tr">Reintentar</button>';
          tradeMsg(errorHtml);
          if (retryable) armTradeRetry();
          try { U.showToast("error", "No ha salido bien. Revisa el aviso."); } catch (e) {}
          return;
        }
        if (res.data && res.data.executed) {
          var evidence = res.data.quote || {};
          tradeMsg('<div class="state ok" role="status"><strong>' + U.esc(tradeSide === "sell" ? "Venta" : "Compra") +
            ' lista.</strong> ' + U.esc(fmtP(res.data.price, market)) + ' · ' +
            U.esc(String(evidence.provider || "").toUpperCase()) +
            (evidence.approximate ? ' · aproximado' : '') + '. Ya aparece marcada en el gráfico.</div>');
          try { U.showToast("ok", (tradeSide === "sell" ? "Venta" : "Compra") + " lista."); } catch (e) {}
          paintMarkers();
          loadSnapshot();
          paintTradePair();
          refreshEntryPlan(); /* M3: la posición cambió con la operación */
          return;
        }
        var ctx;
        try {
          var availTxt = "—";
          try { availTxt = U.fmtQty(lastAvailQty, parts.asset); } catch (e2) { availTxt = "—"; }
          if (lastAvailQty === null || lastAvailQty === undefined) availTxt = "—";
          var sellEstimate = tradeSide === "sell" && window.OrbisDecimal &&
            window.OrbisDecimal.estimateSell(qty, "0", String(lastTradeQuote && lastTradeQuote.price));
          ctx = (tradeSide === "sell")
            ? { qty: U.fmtQty(qty, parts.asset), avail: availTxt, asset: parts.asset,
                fee: U.fmtMoney("0", parts.quote),
                amount: sellEstimate ? U.fmtMoney(sellEstimate.net, parts.quote) : "—" }
            : { amount: U.fmtMoney(qty, parts.quote), cash: "menos", fee: U.fmtMoney("0", parts.quote), max: "configurado", maxFee: "configurado", asset: parts.asset };
        } catch (e) { ctx = { asset: parts.asset }; }
        var ph = null;
        try { ph = U.guardPhrase(res.data && res.data.reason, ctx); } catch (e) { ph = null; }
        tradeMsg('<div class="amber" role="status"><strong>' + U.esc(tradeSide === "sell" ? "Venta" : "Compra") +
          " no se ha podido hacer.</strong> " + U.esc(ph || "Motivo no reconocido; revisa tu saldo e inténtalo de nuevo.") +
          ' <button type="button" class="ghost" data-retry="st-tr">Reintentar</button></div>');
        armTradeRetry();
        try { U.showToast("blocked", ph || "No se ha podido hacer."); } catch (e) {}
      }).catch(function () {
        if (mkAtSend !== market || !$("st-trade-msg") || !chartBox.isConnected) { btn.disabled = false; return; }
        btn.disabled = false;
        var nb = "No hemos podido conectar. ¿Está el servidor arrancado?";
        try { nb = U.networkBox("st-tr"); } catch (e) {}
        tradeMsg(nb);
        armTradeRetry();
        try { U.showToast("error", "No hemos podido conectar."); } catch (e) {}
      });
    }

    /* Tanda C2: meta-precio (#st-meta) en UN solo punto de formateo (pura
     * salvo U/fmtP, testeable en Node con U simulado). La usan loadCandles,
     * ws.onmessage y refreshOpenCandle con el mismo HTML: mercado ·
     * proveedor · precio (o "Sin datos") + " · Inicio del historial" si
     * showStart. NO se copia el bloque: los tres caminos llaman aquí. */
    function stMetaHtml(mk, rp, px, n, showStart) {
      return '<p class="muted"><strong>' + U.esc(mk) + "</strong> · " +
        U.esc(String(rp)) +
        (n ? " · " + fmtP(px, mk) : " · Sin datos") +
        (showStart ? " · Inicio del historial" : "") + "</p>";
    }
    function updateCandleQuality() {
      var badge = $("st-quality");
      if (!badge) return;
      badge.hidden = true;
      badge.textContent = "";
      badge.removeAttribute("title");
      if (archiveRequested || candles.length < 2) return;
      var count = 0;
      var first = null;
      for (var i = 1; i < candles.length; i++) {
        var previous = candles[i - 1].time;
        var expected;
        if (timeframe === "1M") {
          var month = new Date(previous * 1000);
          expected = Date.UTC(month.getUTCFullYear(), month.getUTCMonth() + 1, 1) / 1000;
        } else {
          expected = previous + tfSeconds(timeframe);
        }
        if (candles[i].time > expected) {
          count++;
          if (!first) first = [expected, candles[i].time];
        }
      }
      if (!count) return;
      badge.textContent = "⚠ " + count + (count === 1 ? " tramo sin velas" : " tramos sin velas");
      badge.title = "Primer hueco: " + new Date(first[0] * 1000).toISOString() +
        " → " + new Date(first[1] * 1000).toISOString() + ". No se inventan precios.";
      badge.hidden = false;
    }
    function resetArchiveDisplay() {
      archiveRequested = timeframe === "1d" && ["BTC/USD", "BTC/EUR"].includes(market) &&
        ["auto", "bitstamp"].includes(provider) && ["5", "10", "all", "custom"].includes($("st-range").value);
      $("st-archive-meta").replaceChildren();
      $("st-archive-year-wrap").hidden = true;
      $("st-archive-year").innerHTML = '<option value="">Elegir</option>';
      delete chartBox.dataset.archiveWindow;
      delete chartBox.dataset.archiveFrom;
      delete chartBox.dataset.archiveTo;
    }
    function requestedWindow(period, last) {
      if (period === "custom") {
        return {
          from: Date.parse($("st-range-from").value + "T00:00:00Z") / 1000,
          to: Date.parse($("st-range-to").value + "T00:00:00Z") / 1000 + 86400
        };
      }
      return {from:period === "all" ? null : window.OrbisMarketHistory.yearsBefore(last, Number(period)), to:null};
    }
    function studyCandleEnd(time) {
      return candleEndTime(time, tfSeconds(timeframe), timeframe === "1M");
    }
    function inRequestedWindow(candle, requested) {
      return candle.time < requested.to && studyCandleEnd(candle.time) > requested.from;
    }
    function loadCandles(backfill) {
      if (loading) return;
      loading = true;
      var generation = loadGeneration;
      var period = $("st-range").value;
      function current() { return generation === loadGeneration && chartBox.isConnected; }
      $("st-loading").hidden = false;
      /* El límite before es exclusivo: nunca mezclar velas nuevas con el
       * bloque anterior ni cambiar de proveedor a mitad de un gráfico. */
      var before = null;
      var oldest = null;
      if (backfill && candles.length) {
        oldest = candles[0].time;
        for (var oi = 1; oi < candles.length; oi++) if (candles[oi].time < oldest) oldest = candles[oi].time;
        before = oldest;
      }
      var requestLimit = period === "current" ? candleLimitFor(indCfg) : 1000;
      var candlesUrl = "/market/candles?symbol=" + encodeURIComponent(market) +
        "&timeframe=" + encodeURIComponent(timeframe) + "&limit=" + requestLimit;
      function candlePage(source, cursor) {
        return U.api(candlesUrl + "&provider=" + encodeURIComponent(source) +
          (cursor ? "&before=" + cursor : ""), {cache:"no-store"});
      }
      var initial = archiveRequested && !backfill ?
        U.api("/market/btc-daily-archive?quote_currency=" + encodeURIComponent(market.split("/")[1]), {cache:"no-store"}) :
        candlePage(backfill ? (chartResolvedProvider || provider) : provider, before);
      initial.then(function (res) {
        var data = res.ok && res.data;
        if (!current() || backfill || archiveRequested || period === "current" || !data ||
            !data.available || !data.older_pagination_supported ||
            !Array.isArray(data.candles) || !data.candles.length) return res;
        var target = requestedWindow(period, Number(data.candles[data.candles.length - 1].t)).from;
        return window.OrbisMarketHistory.collect(data, function (cursor) {
          return candlePage(data.resolved_provider, cursor);
        }, {symbol:market, timeframe:timeframe, current:current,
          reached:function (oldest) { return target !== null && oldest <= target; }})
          .then(function (history) {
            return {ok:true, data:Object.assign({}, data, {candles:history.rows}), history:history};
          });
      }).then(function (res) {
        if (!current()) return;
        loading = false;
        $("st-loading").hidden = true;
        if (!res.ok) {
          var msg = (res.data && (res.data.reason || res.data.detail)) || "no disponible";
          if (backfill) {
            hasMore = false;
            $("st-archive-meta").textContent = "No se pudo cargar historia anterior: " + String(msg);
            return;
          }
          if ($("st-tf-note")) $("st-tf-note").textContent = String(msg);
          $("st-meta").innerHTML = '<div class="state empty" role="status"><strong>No disponible.</strong> ' + U.esc(String(msg)) +
            ' <button type="button" class="ghost" id="st-retry">Reintentar</button> <button type="button" class="ghost" id="st-chprov">Cambiar proveedor</button></div>';
          var rb = $("st-retry"), cb = $("st-chprov");
          if (rb) rb.addEventListener("click", function () { loadAll(); });
          if (cb) cb.addEventListener("click", function () { $("st-prov").focus(); });
          return;
        }
        /* El backend rechaza TF no nativos con HTTP 200 + available:false y
         * motivo honesto (Fase 4: kraken/1s, binance/30s…): mostrarlo igual
         * que un error en vez de un "Sin datos" genérico que lo esconde. */
        if (res.data && res.data.available === false) {
          var msgU = String(res.data.reason || "no disponible");
          if (backfill) {
            hasMore = false;
            $("st-archive-meta").textContent = msgU;
            return;
          }
          var failProv = (res.data && res.data.resolved_provider) || provider;
          var failTf = (res.data && res.data.timeframe) || timeframe;
          /* M5: el fallo honesto alimenta la caché observada y el mismo
           * updateTfAvailability deshabilita la opción con el motivo exacto. */
          try { markTfUnavailable(failProv, failTf, msgU); } catch (e) {}
          try { updateTfAvailability(); } catch (e) {}
          if ($("st-tf-note")) $("st-tf-note").textContent = msgU;
          $("st-meta").innerHTML = '<div class="state empty" role="status"><strong>No disponible.</strong> ' + U.esc(msgU) +
            ' <button type="button" class="ghost" id="st-retry">Reintentar</button> <button type="button" class="ghost" id="st-chprov">Cambiar proveedor</button></div>';
          var rbU = $("st-retry"), cbU = $("st-chprov");
          if (rbU) rbU.addEventListener("click", function () { loadAll(); });
          if (cbU) cbU.addEventListener("click", function () { $("st-prov").focus(); });
          return;
        }
        if (backfill && chartResolvedProvider && (!res.data || res.data.resolved_provider !== chartResolvedProvider)) {
          hasMore = false;
          $("st-archive-meta").textContent = "La fuente histórica cambió; se conserva el gráfico sin mezclar precios.";
          return;
        }
        var rows = ((res.data && res.data.candles) || []).map(mapCandleRow);
        if (backfill && candles.length) {
          var have = {};
          candles.forEach(function (c) { have[c.time] = 1; });
          var fresh = rows.filter(function (c) { return !have[c.time]; });
          /* La respuesta de historia se cruza con el límite exclusivo; un
           * proveedor que devuelva solape no altera el gráfico. */
          var older = (oldest !== null && oldest !== undefined)
            ? fresh.filter(function (c) { return c.time < oldest; })
            : fresh;
          if (older.length) {
            candles = older.concat(candles).sort(function (a, b) { return a.time - b.time; });
            paintAll(true, older.length);
            if (period !== "current") {
              $("st-archive-meta").textContent = "Cobertura cargada: " +
                new Date(candles[0].time * 1000).toISOString().slice(0, 10) + " → " +
                new Date(candles[candles.length - 1].time * 1000).toISOString().slice(0, 10) + ".";
            }
          }
          /* Una página corta no prueba el final: muchos proveedores imponen
           * su propio máximo. Sólo paramos cuando la fuente lo confirma o
           * el cursor deja de avanzar. */
          if (!older.length || res.data.history_exhausted) hasMore = false;
        } else {
          candles = rows.sort(function (a, b) { return a.time - b.time; });
          hasMore = res.history ? res.history.hasMore :
            !archiveRequested && Boolean(res.data.older_pagination_supported) &&
            rows.length > 0 && !res.data.history_exhausted;
          paintAll(false);
          if (archiveRequested && candles.length) {
            var years = Array.from(new Set(candles.map(function (c) {
              return new Date(c.time * 1000).getUTCFullYear();
            })));
            $("st-archive-year").innerHTML = '<option value="">Elegir</option>' + years.map(function (year) {
              return '<option value="' + year + '">' + year + '</option>';
            }).join("");
            $("st-archive-year-wrap").hidden = false;
            showArchivePeriod($("st-range").value);
          } else if ($( "st-range").value !== "current" && candles.length) {
            showArchivePeriod($("st-range").value);
          }
        }
        var rp = (res.data && res.data.resolved_provider) || provider;
        updateCandleQuality();
        if (!backfill) {
          chartResolvedProvider = rp;
          /* AUTO queda fijado al proveedor de las velas durante esta sesión.
           * El ticker y WS no deben volver a resolver otro venue. */
          startStream();
        }
        lastResolvedProvider = rp;
        var okTf = (res.data && res.data.timeframe) || timeframe;
        /* M5: el éxito rehabilita el par observado (si alguna vez falló) y
         * la nota observada se mantiene visible tras cargar (no se borra):
         * el selector deshabilita con motivo lo que el backend rechazó. */
        try { markTfAvailable(rp, okTf); } catch (e) {}
        try { updateTfAvailability(); } catch (e) { if ($("st-tf-note")) $("st-tf-note").textContent = ""; }
        lastPrice = candles.length ? num(candles[candles.length - 1].close) : null;
        $("st-meta").innerHTML = stMetaHtml(market, String(rp).toUpperCase(), lastPrice, candles.length, backfill && !hasMore);
        if (!backfill && !archiveRequested && !res.data.older_pagination_supported && candles.length) {
          $("st-archive-meta").textContent = "Esta fuente no permite cargar velas anteriores. Puedes elegir otra fuente para ampliar el histórico.";
        }
        if (archiveRequested && res.data && res.data.available) {
          var firstDay = new Date(res.data.first_candle_at * 1000).toISOString().slice(0, 10);
          var lastDay = new Date(res.data.last_candle_at * 1000).toISOString().slice(0, 10);
          $("st-archive-meta").innerHTML = '<p class="muted" data-help="Velas diarias cerradas de Bitstamp; las fechas sin negociación no se rellenan. El precio en el gráfico es el cierre histórico, distinto del precio actual.">Archivo ' + U.esc(firstDay) + ' → ' + U.esc(lastDay) + ' · ' + candles.length + ' velas · ' + Number(res.data.missing_days || 0) + ' días sin vela · ' + U.esc(market) + '</p>';
          if (res.data.stale) {
            $("st-archive-meta").innerHTML += '<p class="amber" role="status">Copia local sin actualizar. Última vela: ' + U.esc(lastDay) + '.</p>';
          }
          if ($("st-range").value === "custom") {
            var fromRequested = Date.parse($("st-range-from").value + "T00:00:00Z") / 1000;
            var toRequested = Date.parse($("st-range-to").value + "T23:59:59Z") / 1000;
            if (candles.length && (candles[0].time > fromRequested ||
                candles[candles.length - 1].time < toRequested - 86400)) {
              $("st-archive-meta").innerHTML += '<p class="amber" role="status">Parte del rango solicitado queda fuera del archivo.</p>';
            }
          }
        }
        if (!backfill && !archiveRequested && period !== "current" && candles.length) {
          var firstAvailable = candles[0].time;
          var lastAvailable = candles[candles.length - 1].time;
          var requested = requestedWindow(period, lastAvailable);
          var allAvailable = period === "all";
          var incomplete = requested.from !== null && (firstAvailable > requested.from ||
            (requested.to !== null && studyCandleEnd(lastAvailable) < requested.to));
          var messages = [];
          if (allAvailable || incomplete) {
            messages.push((incomplete ? "Cobertura parcial: " : "Cobertura cargada: ") +
              new Date(firstAvailable * 1000).toISOString().slice(0, 10) + " → " +
              new Date(lastAvailable * 1000).toISOString().slice(0, 10) + ".");
          }
          var issue = res.history && {
            limit: "Límite de carga automática. Desplaza el gráfico a la izquierda para seguir.",
            source: "No se pudo continuar con la misma fuente e intervalo.",
            cursor: "La fuente no avanzó de forma coherente."
          }[res.history.problem];
          if (issue) messages.push(issue);
          else if (allAvailable && !(res.history && res.history.exhausted) && !res.data.history_exhausted)
            messages.push("Puede existir más histórico en la fuente.");
          if (studyCandleEnd(lastAvailable) < Date.now() / 1000 - 2 * 86400) {
            messages.push("Faltan velas recientes de esta fuente.");
          }
          if (period === "custom" && !candles.some(function (c) { return inRequestedWindow(c, requested); }))
            messages.push("No hay velas cargadas entre esas fechas.");
          if (timeframe !== "1d" && ["BTC/USD", "BTC/EUR"].includes(market) && ["auto", "bitstamp"].includes(provider))
            messages.push((market === "BTC/USD" ? "Archivo desde 2012" : "Archivo desde 2016") + " · elige 1d.");
          if (messages.length) $("st-archive-meta").textContent = messages.join(" ");
        }
        loadSnapshot();
      }).catch(function () {
        if (generation !== loadGeneration || !chartBox.isConnected) return;
        loading = false;
        $("st-loading").hidden = true;
        $("st-meta").innerHTML = '<div class="state error" role="alert"><strong>No hemos podido conectar.</strong> <button type="button" class="ghost" id="st-retry2">Reintentar</button></div>';
        var rb = $("st-retry2");
        if (rb) rb.addEventListener("click", function () { loadAll(); });
      });
    }
    function checkBackfill() {
      if (!chart || loading || !hasMore || !candles.length) return;
      if ($("st-range").value !== "current" && Date.now() - lastHistoryGesture > 1000) return;
      var range = null;
      try { range = chart.timeScale().getVisibleLogicalRange(); } catch (e) {}
      if (range && range.from < 5) loadCandles(true);
    }
    function loadSnapshot() {
      var generation = loadGeneration;
      var requestedMarket = market, requestedProvider = provider;
      if (requestedProvider === "auto" && !chartResolvedProvider) return;
      var snapshotProvider = chartResolvedProvider || requestedProvider;
      var sourceLine = $("st-price-source");
      function unavailable() {
        lastTradeQuote = null;
        lastTradeQuoteReceivedMs = null;
        syncTradePrice();
        paintTradePair();
        if (!sourceLine) return;
        sourceLine.hidden = false;
        sourceLine.dataset.state = "stale";
        sourceLine.textContent = "Cotización sin actualizar";
        sourceLine.title = "No se pudo confirmar una nueva lectura del proveedor.";
      }
      /* no-store como en las velas: el ticker también se consulta cada
       * segundo y la URL es estable (ver refreshOpenCandle). */
      U.api("/market/snapshot?symbol=" + encodeURIComponent(market) + "&provider=" + encodeURIComponent(snapshotProvider), { cache: "no-store" }).then(function (r) {
        if (generation !== loadGeneration || requestedMarket !== market || requestedProvider !== provider) return;
        if (!r.ok || !r.data) { unavailable(); return; }
        var d = r.data;
        if (d.resolved_provider && d.resolved_provider !== snapshotProvider) { unavailable(); return; }
        if (d.available) {
          lastTradeQuote = d;
          lastTradeQuoteReceivedMs = performance.now();
          syncTradePrice();
          lastPrice = num(d.price);
          lastBid = d.bid; lastAsk = d.ask;
          lastResolvedProvider = d.resolved_provider || provider; /* M4: provenance del helper */
          /* Cuando el proveedor no publica libro de órdenes se dice con
           * palabras, no con guiones sueltos que parecen un dato roto. */
          var hayCompra = d.bid !== null && d.bid !== undefined;
          var hayVenta = d.ask !== null && d.ask !== undefined;
          $("st-depth").textContent = (hayCompra || hayVenta)
            ? (hayCompra ? "Compra " + fmtP(d.bid, market) : "Compra no disponible") +
              " · " + (hayVenta ? "Venta " + fmtP(d.ask, market) : "Venta no disponible") +
              (d.change_pct !== null && d.change_pct !== undefined ? " · Cambio diario " + num(d.change_pct) + "%" : "")
            : "Último precio " + fmtP(d.price, market) +
              (d.change_pct !== null && d.change_pct !== undefined ? " · Cambio diario " + num(d.change_pct) + "%" : "") +
              ". Este mercado no publica precios de compra ni de venta.";
          if (sourceLine) {
            var stamp = d.market_time_status || "unknown";
            var age = Number(d.market_age_seconds);
            var ageText = Number.isFinite(age) && d.market_age_seconds !== null && d.market_age_seconds !== undefined
              ? (age < 60 ? age + " s" : Math.floor(age / 60) + " min") : "—";
            var label = stamp === "fresh" ? "mercado hace " + ageText :
              stamp === "stale" ? "dato atrasado · " + ageText :
              stamp === "future" ? "hora futura del mercado" :
              stamp === "invalid" ? "hora del mercado inválida" : "hora del mercado no publicada";
            sourceLine.hidden = false;
            sourceLine.dataset.state = stamp;
            sourceLine.textContent = String(d.resolved_provider || provider).toUpperCase() + " · " + label;
            var detail = "Consulta local: " + String(d.received_at || "sin hora") +
              ". Hora del mercado: " + String(d.market_time_at || "no publicada") +
              ". Consultar no confirma por sí solo que el precio sea ejecutable.";
            sourceLine.title = detail;
            sourceLine.setAttribute("data-help", detail);
            sourceLine.setAttribute("aria-description", detail);
          }
          paintTradePair(); /* M4: par visible con precio fresco */
          paintEntryLines(); /* M3: la etiqueta % sigue al ticker */
        } else {
          $("st-depth").textContent = U.esc ? String(d.reason || "no disponible") : "no disponible";
          unavailable();
        }
      }).catch(function () {
        if (generation === loadGeneration && requestedMarket === market && requestedProvider === provider) unavailable();
      });
    }

    /* Refresco incremental de la vela abierta (Fase 4): el temporizador ya
     * no trae solo el ticker (loadSnapshot); también trae las últimas velas
     * y las funde con mergeCandleRows (misma fila mapeada que loadCandles).
     * - Solo cambia la última (vela abierta): update in situ + indicadores +
     *   dibujos, sin recargar todo.
     * - Llegaron velas nuevas: paintAll(true) conservando la vista.
     * Se piden las últimas 5 SIN since: el parámetro since no significa lo
     * mismo en cada exchange (en OKX `after` pagina hacia atrás, así que con
     * since=última nunca llegaría una vela nueva) y todos devuelven su
     * ventana más reciente sin él; la fusión por tiempo hace el resto.
     * No pisa una carga en curso (loading), no pide sin velas previas y no
     * solapa peticiones (refreshing). La caché de indicadores invalida solo
     * si cambió el último cierre, así que el repinte por tick es barato.
     * Si el proveedor deja de servir, no se inventa nada (se ignora). */
    var refreshing = false, refreshLastClose = null;
    function refreshOpenCandle() {
      if (loading || refreshing || !candles.length || !chart || !priceSeries) return;
      refreshing = true;
      var generation = loadGeneration;
      /* Guarda WS↔REST (Fase 4b): se marca la secuencia de ticks y el tiempo
       * de la última vela ANTES de pedir; si al responder llegó un tick
       * (`wsTickSeq` cambió, ver ws.onmessage), el REST puede ser más viejo
       * y solo se aceptan tiempos estrictamente nuevos (sin retroceso). La
       * guarda reusa `generation`/`refreshing`/`loading` ya existentes. */
      var wsMark = wsTickSeq;
      var lastT = candles[candles.length - 1].time;
      /* no-store: sin esto el navegador sirve la primera respuesta de su
       * caché heurística (la URL es idéntica en cada tick y el API no manda
       * Cache-Control) y la vela quedaba congelada aunque el temporizador
       * corriese. La URL sigue estable para que la caché del backend
       * absorba (TTL por timeframe, 1/4 de vela: sin martillear el exchange). */
      U.api("/market/candles?symbol=" + encodeURIComponent(market) +
        "&provider=" + encodeURIComponent(chartResolvedProvider || provider) + "&timeframe=" + encodeURIComponent(timeframe) + "&limit=5", { cache: "no-store" }).then(function (res) {
        refreshing = false;
        if (generation !== loadGeneration) return;
        if (!res.ok || !res.data || !res.data.available) return;
        if (chartResolvedProvider && res.data.resolved_provider !== chartResolvedProvider) return;
        var rows = ((res.data && res.data.candles) || []).map(mapCandleRow);
        if (!rows.length) return;
        if (wsTickSeq !== wsMark) rows = pruneRefreshRows(lastT, rows, true);
        var oldN = candles.length;
        var m = mergeCandleRows(candles, rows);
        if (m.grew) updateCandleQuality();
        lastPrice = num(candles[candles.length - 1].close);
        /* Tanda C2: el meta-precio también sigue al refresco de la vela
         * abierta (mismo stMetaHtml que loadCandles y el tick WS). */
        try { $("st-meta").innerHTML = stMetaHtml(market, String(chartResolvedProvider || provider).toUpperCase(), lastPrice, candles.length, false); } catch (e) {}
        if (m.grew) {
          /* Seguir en vivo: si la vista ya estaba en el borde derecho, se
           * desplaza tantas velas como llegaron; si se inspecciona el
           * histórico, la vista no se mueve (paintAll(true) la conserva). */
          var k = candles.length - oldN, vr = null;
          try { vr = chart.timeScale().getVisibleLogicalRange(); } catch (e) {}
          refreshLastClose = String(candles[candles.length - 1].close);
          paintAll(true);
          if (vr && vr.to >= oldN - 1.5) {
            try { chart.timeScale().setVisibleLogicalRange({ from: vr.from + k, to: vr.to + k }); } catch (e) {}
            redrawDrawings();
          }
        } else {
          var last = candles[candles.length - 1];
          if (String(last.close) === refreshLastClose) return; /* sin cambios: no repintar */
          refreshLastClose = String(last.close);
          try {
            priceSeries.update(latestSeriesPoint());
          } catch (e) {}
          paintIndicators(); /* la caché solo recalcula si cambió el cierre */
          redrawDrawings();
          paintEntryLines(); /* M3: la etiqueta % cambia con el precio; la de entrada NO se mueve */
        }
      }).catch(function () { refreshing = false; });
    }
    /* Un solo tick de refresco (Fase 4): ticker (precio/bid/ask) + vela
     * abierta e indicadores. Lo usan el temporizador Auto/manual y el
     * fallback sin WebSocket, para que ambos caminos refresquen lo mismo. */
    function refreshEstudioTick() {
      loadSnapshot();
      refreshOpenCandle();
    }

    /* ---------------- streaming WS + fallback SSE/poll ---------------- */
    function stopStream() {
      stopping = true; /* cierre intencional: onclose no debe reprogramar nada */
      var s = ws;
      ws = null;
      try { if (s) s.close(); } catch (e) {}
      if (pollTimer) { try { clearTimeout(pollTimer); } catch (e) {} try { clearInterval(pollTimer); } catch (e) {} pollTimer = null; }
    }
    function startStream() {
      stopStream();
      if (archiveRequested) return; /* archivo cerrado: ticks en vivo no alteran velas históricas */
      if (provider === "grvt" || market.endsWith(":PERP")) return; /* GRVT: sin sondeo REST implícito, incluso con AUTO. */
      stopping = false; /* a partir de aquí los cierres vuelven a ser ajenos */
      var delays = [1, 2, 4, 8];
      function connect() {
        if (stopping) return;
        var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host +
          "/market/stream?symbol=" + encodeURIComponent(market) + "&provider=" + encodeURIComponent(chartResolvedProvider || provider);
        try { ws = new WebSocket(url); } catch (e) { fallbackPoll(); return; }
        var my = ws;
        ws.onopen = function () { wsAttempt = 0; };
        ws.onmessage = function (ev) {
          if (ws !== my) return;
          var d = null;
          try { d = JSON.parse(ev.data); } catch (e) { return; }
          if (d && chartResolvedProvider && d.resolved_provider && d.resolved_provider !== chartResolvedProvider) return;
          if (d && d.available && d.price) {
            var receivedMs = Date.parse(d.received_at || "");
            var wallAgeMs = Date.now() - receivedMs;
            if (!isFinite(receivedMs) || wallAgeMs > 60000 || wallAgeMs < -120000 ||
                ["stale", "future", "invalid"].indexOf(d.market_time_status) >= 0 ||
                !isFinite(Number(d.price)) || !(Number(d.price) > 0)) return;
            var eventMs = d.market_time_status === "fresh" ? Date.parse(d.market_time_at || "") : NaN;
            var t = Math.floor((isFinite(eventMs) ? eventMs : receivedMs) / 1000);
            lastPrice = num(d.price);
            var lastBarTime = candles.length ? Number(candles[candles.length - 1].time) : NaN;
            var calendarTf = timeframe === "1M" || timeframe === "1w";
            var nextBarTime = calendarTf ? nextCandleStart(lastBarTime, tfSeconds(timeframe), timeframe) : null;
            if (calendarTf && nextBarTime !== null && t >= nextBarTime) {
              /* No crear OHLC semanal/mensual parcial desde un único ticker:
               * espera la nueva vela de la misma fuente pública. */
              refreshOpenCandle();
            } else if (candles.length && t >= lastBarTime &&
                       (calendarTf || t - lastBarTime < 60 * 60 * 24)) {
              /* Tanda C3: el tick rola la vela si su tiempo ya pertenece a
               * la siguiente (frontera de TF); si no, muta la última como
               * siempre (sin duplicar lógica: vive en rollCandleIfNeeded).
               * Tanda C2: el meta-precio sigue a cada tick con el MISMO
               * formateo que loadCandles (stMetaHtml). */
              var oldCount = candles.length;
              rollCandleIfNeeded(candles, { t: t, price: d.price }, tfSeconds(timeframe), timeframe);
              if (candles.length !== oldCount) updateCandleQuality();
              var last = candles[candles.length - 1];
              wsTickSeq++; /* marca para refreshOpenCandle: hubo tick en vuelo */
              try { $("st-meta").innerHTML = stMetaHtml(market, String(chartResolvedProvider || provider).toUpperCase(), lastPrice, candles.length, false); } catch (e) {}
              if (priceSeries) {
                try {
                  priceSeries.update(latestSeriesPoint());
                } catch (e) {}
              }
              /* Los indicadores siguen a la vela en cada tick (Fase 4): la
               * caché de la Fase 1 solo recalcula si cambió el último cierre,
               * así que el repinte es barato (reutiliza arrays). Sin esto la
               * EMA/SMA/BB/RSI/MACD quedaban estancadas hasta el siguiente
               * evento de recarga. */
              paintIndicators();
              redrawDrawings();
              paintEntryLines(); /* M3: la etiqueta % sigue a cada tick WS */
            }
          }
        };
        ws.onclose = function () {
          if (stopping) return; /* cierre intencional (stopStream): no reconectar */
          if (ws === my) ws = null;
          else return; /* cierre de una instancia vieja: no matar ni reprogramar la nueva (loadAll rápido) */
          var wait = wsAttempt < delays.length ? delays[wsAttempt] : 8;
          if (wait > 30) wait = 30;
          wsAttempt++;
          pollTimer = setTimeout(function () {
            if (stopping) return;
            if (!document.hidden) connect();
            else pollTimer = setTimeout(function () { if (!stopping) connect(); }, 8000);
          }, wait * 1000);
        };
        ws.onerror = function () { if (stopping) return; try { my.close(); } catch (e) {} if (ws === my) { ws = null; fallbackPollOnce(); } };
      }
      function fallbackPollOnce() { refreshEstudioTick(); }
      function fallbackPoll() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(function () { if (!document.hidden) refreshEstudioTick(); }, 8000);
      }
      try { connect(); } catch (e) { fallbackPoll(); }
    }

    /* ---------------- dibujos: interacción ----------------
     * Conversión SVG<->datos alineada con LWC v5: tx/tAt usan
     * chart.timeScale().timeToCoordinate/coordinateToTime cuando existe;
     * solo si la API no está disponible
     * se usa el fallback por índice lineal. py/pAt usan
     * priceToCoordinate/coordinateToPrice. Todos los dibujos
     * (tendencia/fib/fibext/zona/medidor/long-short/texto/h/v/alerta) y el
     * preview comparten esta misma conv (conv.tx/conv.py/conv.tAt/conv.pAt). */
    function svgConv() {
      var r = chartBox.getBoundingClientRect();
      var w = r.width || 600;
      var times = candles.map(function (c) { return c.time; });
      var n = Math.max(1, times.length);
      /* Ventana lógica visible compartida con el vendor: from cae en el
       * margen izquierdo y to en el derecho. Sin rango, vista completa. */
      function visRange() {
        /* Normalización propia de la ventana (orden + span mínimo 0.5):
         * el motor valida el rango (from<=to) y la capa SVG (idxTx/idxTAt)
         * debe usar una ventana estable o los dibujos se desalinean de las
         * velas tras pan/zoom agresivos. */
        try {
          if (chart && chart.timeScale) {
            var rr = chart.timeScale().getVisibleLogicalRange();
            if (rr && isFinite(+rr.from) && isFinite(+rr.to)) {
              var a = +rr.from, b = +rr.to;
              if (b < a) { var t = a; a = b; b = t; }
              if (b - a < 0.5) { var m = (a + b) / 2; a = m - 0.25; b = m + 0.25; }
              return { from: a, to: b };
            }
          }
        } catch (e) {}
        return { from: 0, to: n - 1 };
      }
      function idxTx(t) {
        var i = times.indexOf(t);
        if (i < 0) {
          var best = 0, bd = 1e18;
          for (var k = 0; k < times.length; k++) {
            var d = Math.abs(times[k] - t);
            if (d < bd) { bd = d; best = k; }
          }
          i = best;
        }
        var vr = visRange(), span = vr.to - vr.from;
        if (!(span > 0)) return 8 + (w - 72) / 2;
        return 8 + (i - vr.from) / span * (w - 72);
      }
      function idxTAt(x) {
        var vr = visRange(), span = vr.to - vr.from;
        if (!(span > 0)) return times[0];
        var frac = (x - 8) / Math.max(1, (w - 72));
        var i = Math.round(vr.from + frac * span);
        i = Math.max(0, Math.min(n - 1, i));
        return times[i];
      }
      function tsApi() {
        try { if (chart && chart.timeScale) return chart.timeScale(); } catch (e) {}
        return null;
      }
      return {
        tx: function (t) {
          var ts = tsApi();
          if (ts && typeof ts.timeToCoordinate === "function") {
            try {
              var x = ts.timeToCoordinate(t);
              if (typeof x === "number" && isFinite(x)) return x;
            } catch (e) {}
          }
          return idxTx(t);
        },
        py: function (p) {
          if (!priceSeries || !priceSeries.priceToCoordinate) return 0;
          var y = priceSeries.priceToCoordinate(num(p));
          return y === null || y === undefined ? 0 : y;
        },
        tAt: function (x) {
          var ts = tsApi();
          if (ts && typeof ts.coordinateToTime === "function") {
            try {
              var t = ts.coordinateToTime(x);
              if (typeof t === "number" && isFinite(t)) return t;
            } catch (e) {}
          }
          /* Fallback honesto: índice lineal si coordinateToTime no devuelve
           * un tiempo. coordinateToLogical no está en la API estable
           * pública v5 confirmada; aquí necesitamos un timestamp, no un índice. */
          return idxTAt(x);
        },
        pAt: function (y) {
          if (!priceSeries || !priceSeries.coordinateToPrice) return 0;
          return priceSeries.coordinateToPrice(y);
        }
      };
    }
    function magnetize(p) {
      if (!magnet || !candles.length) return p;
      var best = null, bd = 1e18;
      candles.forEach(function (c) {
        [c.open, c.high, c.low, c.close].forEach(function (v) {
          var d = Math.abs(num(v) - num(p));
          if (d < bd) { bd = d; best = num(v); }
        });
      });
      return best === null ? p : best;
    }
    function redrawDrawings() {
      if (!drawSvg || !priceSeries || !candles.length) return;
      var conv = svgConv();
      var r = chartBox.getBoundingClientRect();
      drawSvg.setAttribute("viewBox", "0 0 " + Math.round(r.width || 600) + " " + Math.round(chartBox.clientHeight || 380));
      drawSvg.style.height = (chartBox.clientHeight || 380) + "px";
      var html = "";
      drawings.forEach(function (d) {
        if (d.hidden) return;
        html += drawOne(d, conv);
      });
      drawSvg.innerHTML = html;
    }
    function escA(v) { return String(v === null || v === undefined ? "" : v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;"); }
    function line(x1, y1, x2, y2, color, dash, w) {
      return '<line x1="' + x1.toFixed(1) + '" y1="' + y1.toFixed(1) + '" x2="' + x2.toFixed(1) + '" y2="' + y2.toFixed(1) +
        '" stroke="' + color + '" stroke-width="' + (w || 1.5) + '"' + (dash ? ' stroke-dasharray="' + dash + '"' : "") + "/>";
    }
    function dot(x, y, sel) {
      return '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="' + (sel ? 5 : 3.5) + '" fill="#4da3ff" stroke="#0e1116"/>';
    }
    function label(x, y, text, color) {
      return '<text x="' + (x + 6).toFixed(1) + '" y="' + (y - 6).toFixed(1) + '" fill="' + (color || "#e6edf3") + '" font-size="11">' + escA(text) + "</text>";
    }
    function drawOne(d, conv) {
      var sel = d.id === selectedId;
      var col = sel ? "#4da3ff" : (d.color || "#e3b341");
      var s = "";
      /* Puntos de control (Fase 3): se pintan SIEMPRE fuera del grupo de
       * opacidad para que el objeto seleccionado siga siendo agarrable
       * aunque su opacidad sea baja. */
      var dots = "";
      var P = function (q) { return { x: conv.tx(q.t), y: conv.py(q.p) }; };
      if (d.type === "h" && d.pts[0]) {
        var y = conv.py(d.pts[0].p);
        s += line(0, y, 1e4, y, col, effDash(d, ""), effWidth(d, 1.5));
        s += label(10, y, fmtP(d.pts[0].p, market));
      } else if (d.type === "v" && d.pts[0]) {
        var x = conv.tx(d.pts[0].t);
        s += line(x, 0, x, 1e4, col, effDash(d, ""), effWidth(d, 1.5));
      } else if ((d.type === "trend" || d.type === "measure" || d.type === "fib" || d.type === "fibext" || d.type === "long" || d.type === "short") && d.pts.length >= 2) {
        var a = P(d.pts[0]), b = P(d.pts[1]);
        if (d.type === "trend") {
          s += line(a.x, a.y, b.x, b.y, col, effDash(d, ""), effWidth(d, 1.5));
          dots += dot(a.x, a.y, sel) + dot(b.x, b.y, sel);
        } else if (d.type === "measure") {
          s += line(a.x, a.y, b.x, b.y, col, effDash(d, "4 3"), effWidth(d, 1.5));
          var p0 = num(d.pts[0].p), p1 = num(d.pts[1].p);
          var pct = p0 ? ((p1 - p0) / Math.abs(p0) * 100) : 0;
          var dt = Math.abs(num(d.pts[1].t) - num(d.pts[0].t));
          s += label(b.x, b.y, fmtP(p1 - p0, market) + " · " + pct.toFixed(2) + "% · " + dt + "s");
          dots += dot(a.x, a.y, sel) + dot(b.x, b.y, sel);
        } else if (d.type === "fib") {
          var lvls = Array.isArray(d.levels) && d.levels.length ? d.levels : fibLevels;
          var hi = Math.max(num(d.pts[0].p), num(d.pts[1].p)), lo = Math.min(num(d.pts[0].p), num(d.pts[1].p));
          var dir = num(d.pts[1].p) >= num(d.pts[0].p) ? 1 : -1;
          lvls.forEach(function (L) {
            var pr = dir > 0 ? hi - (hi - lo) * num(L) : lo + (hi - lo) * num(L);
            var yy = conv.py(pr);
            s += line(Math.min(a.x, b.x), yy, Math.max(a.x, b.x) + 120, yy, col, effDash(d, "5 4"), effWidth(d, 1));
            s += label(Math.max(a.x, b.x) + 4, yy, num(L) + " · " + fmtP(pr, market));
          });
          s += line(a.x, a.y, b.x, b.y, col, effDash(d, ""), effWidth(d, 1.5));
          dots += dot(a.x, a.y, sel) + dot(b.x, b.y, sel);
        } else if (d.type === "fibext" && d.pts.length >= 3) {
          var c = P(d.pts[2]);
          var pA = num(d.pts[0].p), pB = num(d.pts[1].p), pC = num(d.pts[2].p);
          var rng = Math.abs(pB - pA);
          var dirSign = pB >= pA ? 1 : -1;
          var extLevels = Array.isArray(d.levels) && d.levels.length ? d.levels : FIB_EXT;
          extLevels.forEach(function (L) {
            var pr = pC + (dirSign * rng * num(L));
            var yy = conv.py(pr);
            s += line(Math.min(a.x, c.x), yy, Math.max(b.x, c.x) + 120, yy, col, effDash(d, "5 4"), effWidth(d, 1));
            s += label(Math.max(b.x, c.x) + 4, yy, num(L) + " · " + fmtP(pr, market));
          });
          s += line(a.x, a.y, b.x, b.y, col, effDash(d, ""), effWidth(d, 1.5)) + line(b.x, b.y, c.x, c.y, col, effDash(d, "4 3"), effWidth(d, 1.5));
          dots += dot(a.x, a.y, sel) + dot(b.x, b.y, sel) + dot(c.x, c.y, sel);
        } else if (d.type === "long" || d.type === "short") {
          var e = num(d.pts[0].p), st = num(d.pts[1].p), tg = d.pts[2] ? num(d.pts[2].p) : num(d.pts[0].p);
          [e, st, tg].forEach(function (pr, i) {
            var yy = conv.py(pr);
            var cc = i === 0 ? "#4da3ff" : (i === 1 ? "#f85149" : "#3fb950");
            var nm = i === 0 ? "ENTRY" : (i === 1 ? "STOP" : "TARGET");
            s += line(Math.min(a.x, b.x), yy, Math.max(a.x, b.x) + 120, yy, cc, effDash(d, ""), effWidth(d, 1.5));
            s += label(Math.max(a.x, b.x) + 4, yy, nm + " " + fmtP(pr, market));
          });
          var risk = e ? ((st - e) / Math.abs(e) * 100) : 0;
          var rew = e ? ((tg - e) / Math.abs(e) * 100) : 0;
          var R = risk !== 0 ? Math.abs(rew / risk) : 0;
          s += label(a.x, Math.min(a.y, b.y) - 14, "riesgo " + risk.toFixed(2) + "% · +" + rew.toFixed(2) + "% · R" + R.toFixed(1) + " (matemático)");
          dots += dot(a.x, a.y, sel) + dot(b.x, b.y, sel);
        }
      } else if (d.type === "rect" && d.pts.length >= 2) {
        var a2 = P(d.pts[0]), b2 = P(d.pts[1]);
        /* Sin width/style explícitos el marcado es byte-idéntico al anterior
         * (retrocompatibilidad): solo se añaden atributos si se configuraron. */
        var hasW = d && d.width !== undefined && d.width !== null && isFinite(Number(d.width));
        var rDash = effDash(d, "");
        s += '<rect x="' + Math.min(a2.x, b2.x).toFixed(1) + '" y="' + Math.min(a2.y, b2.y).toFixed(1) +
          '" width="' + Math.abs(b2.x - a2.x).toFixed(1) + '" height="' + Math.abs(b2.y - a2.y).toFixed(1) +
          '" fill="rgba(77,163,255,.12)" stroke="' + col + '"' +
          (hasW ? ' stroke-width="' + effWidth(d, 1.5) + '"' : "") +
          (rDash ? ' stroke-dasharray="' + rDash + '"' : "") + "/>";
        dots += dot(a2.x, a2.y, sel) + dot(b2.x, b2.y, sel);
      } else if (d.type === "text" && d.pts[0]) {
        if (d.pinned === false && typeof d.fx === "number" && typeof d.fy === "number") {
          var rr = null;
          try { rr = chartBox.getBoundingClientRect(); } catch (e) {}
          var W = (rr && rr.width) || 600, H = (chartBox.clientHeight || 380);
          var fx = Math.max(0, Math.min(1, d.fx)) * W, fy = Math.max(0, Math.min(1, d.fy)) * H;
          s += label(fx, fy, (d.text || "nota") + " · Flotante", col);
          dots += dot(fx, fy, sel);
        } else {
          var t = P(d.pts[0]);
          s += label(t.x, t.y, d.text || "nota", col);
          dots += dot(t.x, t.y, sel);
        }
      } else if (d.type === "freehand" && d.pts.length >= 2) {
        /* Mano alzada (Fase 3): polilínea en tiempo/precio (nunca píxeles),
         * así que sigue anclada tras zoom/pan/escala. Grosor por defecto 2
         * (trazo de rotulador); respeta color, estilo y opacidad. */
        var fpts = d.pts.map(function (q) { return conv.tx(q.t).toFixed(1) + "," + conv.py(q.p).toFixed(1); }).join(" ");
        var fDash = effDash(d, "");
        s += '<polyline points="' + fpts + '" fill="none" stroke="' + col + '" stroke-width="' + effWidth(d, 2) + '"' +
          (fDash ? ' stroke-dasharray="' + fDash + '"' : "") + ' stroke-linecap="round" stroke-linejoin="round"/>';
        var fe0 = P(d.pts[0]), fe1 = P(d.pts[d.pts.length - 1]);
        dots += dot(fe0.x, fe0.y, sel) + dot(fe1.x, fe1.y, sel);
      } else if (d.type === "alertline" && d.pts[0]) {
        var y2 = conv.py(d.pts[0].p);
        s += line(0, y2, 1e4, y2, d.color || "#d29922", effDash(d, "6 3"), effWidth(d, 1.5));
        s += label(10, y2, "alerta " + fmtP(d.pts[0].p, market), d.color || "#d29922");
      }
      /* Opacidad (Fase 3): solo envuelve cuando <1; con el defecto (1) el
       * marcado es idéntico al anterior (retrocompatibilidad). */
      var op = effOpacity(d);
      if (op < 1) s = '<g opacity="' + op + '">' + s + "</g>";
      return s + dots;
    }

    var pendingPts = [];
    var dragState = null;
    /* Arrastre de objeto existente (Fase 3): {id, part, x0, y0, t0, p0,
     * orig, fx0, fy0, moved}. part>=0 = extremo, part=-1 = cuerpo completo. */
    var editDrag = null;
    var DRAG_MIN_PX = 4;
    var DRAG_TOOLS_2 = { trend: 1, fib: 1, rect: 1, measure: 1 };
    var DRAG_TOOLS_3 = { fibext: 1, long: 1, short: 1 };
    var CLICK_TOOLS = { h: 1, v: 1, text: 1, alertline: 1 };
    /* freehand no entra en DRAG_TOOLS_2: su gesto acumula N puntos (no A→B)
     * y no usa imán (lo deformaría); tiene su propia ruta en los handlers. */
    var TOOL_HINTS = {
      trend: "Tendencia: arrastra de A a B con el ratón (la dirección la eliges tú).",
      fib: "Fib: arrastra de A a B (A→B define directo/inverso, niveles 0–1 en vivo).",
      fibext: "Fib ext: arrastra de A a B y luego clic para C.",
      rect: "Zona: arrastra de A a B para el rectángulo.",
      measure: "Medidor: arrastra de A a B para medir.",
      long: "Escenario alcista: arrastra desde la entrada hasta el límite y después marca el objetivo.",
      short: "Escenario bajista: arrastra desde la entrada hasta el límite y después marca el objetivo.",
      h: "H: clic para la línea horizontal.",
      v: "Vertical: clic para la línea vertical.",
      text: "Texto: clic para colocar la nota.",
      alertline: "Alerta línea: clic para crearla.",
      freehand: "Trazo libre: mantén pulsado y dibuja; al soltar queda guardado y seleccionado."
    };
    function showHint(msg) {
      var box = $("st-ohlc");
      if (!box) return;
      if (!msg) { box.classList.remove("st-hint"); return; }
      box.textContent = msg;
      box.classList.add("st-hint");
    }
    function pointFromEvent(ev) {
      var r = chartBox.getBoundingClientRect();
      var x = ev.clientX - r.left, y = ev.clientY - r.top;
      var conv = svgConv();
      return { x: x, y: y, t: conv.tAt(x), p: magnetize(conv.pAt(y)), conv: conv };
    }
    function previewSvg(tool, A, B, conv) {
      var ax = conv.tx(A.t), ay = conv.py(A.p), bx = conv.tx(B.t), by = conv.py(B.p);
      var s = "";
      if (tool === "trend") {
        s += line(ax, ay, bx, by, "#4da3ff", "6 3", 1.5);
        s += dot(ax, ay, true) + dot(bx, by, true);
      } else if (tool === "fib") {
        var hi = Math.max(num(A.p), num(B.p)), lo = Math.min(num(A.p), num(B.p));
        var dir = num(B.p) >= num(A.p) ? 1 : -1;
        fibLevels.forEach(function (L) {
          var pr = dir > 0 ? hi - (hi - lo) * num(L) : lo + (hi - lo) * num(L);
          var yy = conv.py(pr);
          s += line(Math.min(ax, bx), yy, Math.max(ax, bx) + 120, yy, "#4da3ff", "5 4", 1);
          s += label(Math.max(ax, bx) + 4, yy, num(L) + " · " + fmtP(pr, market), "#4da3ff");
        });
        s += line(ax, ay, bx, by, "#4da3ff", "6 3", 1.5);
        s += dot(ax, ay, true) + dot(bx, by, true);
      } else if (tool === "rect") {
        s += '<rect x="' + Math.min(ax, bx).toFixed(1) + '" y="' + Math.min(ay, by).toFixed(1) +
          '" width="' + Math.abs(bx - ax).toFixed(1) + '" height="' + Math.abs(by - ay).toFixed(1) +
          '" fill="rgba(77,163,255,.18)" stroke="#4da3ff" stroke-dasharray="6 3"/>';
      } else if (tool === "measure") {
        s += line(ax, ay, bx, by, "#4da3ff", "6 3", 1.5);
        var p0 = num(A.p), p1 = num(B.p);
        var pct = p0 ? ((p1 - p0) / Math.abs(p0) * 100) : 0;
        var dt = Math.abs(num(B.t) - num(A.t));
        s += label(bx, by, fmtP(p1 - p0, market) + " · " + pct.toFixed(2) + "% · " + dt + "s", "#4da3ff");
        s += dot(ax, ay, true) + dot(bx, by, true);
      } else if (tool === "fibext" || tool === "long" || tool === "short") {
        s += line(ax, ay, bx, by, "#4da3ff", "6 3", 1.5);
        s += dot(ax, ay, true) + dot(bx, by, true);
        if (tool === "long" || tool === "short") {
          [A.p, B.p].forEach(function (pr, i) {
            var yy = conv.py(pr);
            var cc = i === 0 ? "#4da3ff" : "#f85149";
            var nm = i === 0 ? "ENTRY" : "STOP";
            s += line(Math.min(ax, bx), yy, Math.max(ax, bx) + 120, yy, cc, "6 3", 1.2);
            s += label(Math.max(ax, bx) + 4, yy, nm + " " + fmtP(pr, market), cc);
          });
        }
      }
      return s;
    }
    function paintPreview() {
      if (!dragState || !dragState.moved) return;
      var conv = svgConv();
      var A = dragState.a, B = { t: conv.tAt(dragState.curX), p: magnetize(conv.pAt(dragState.curY)) };
      dragState.b = B;
      redrawDrawings();
      try { drawSvg.innerHTML += previewSvg(dragState.tool, A, B, conv); } catch (e) {}
    }
    /* Constructor puro del modelo de dibujo (Fase 3: extraído de
     * finishObject para probarlo en Node). extra.text evita el prompt en
     * tests; en navegador se sigue pidiendo. */
    function makeDrawing(type, pts, extra) {
      var obj = { id: "d" + Date.now().toString(36) + Math.floor(Math.random() * 1e4), type: type, pts: (pts || []).slice(), color: "#e3b341", locked: false, hidden: false, market: market };
      if (type === "text") {
        var tx = (extra && typeof extra.text === "string") ? extra.text : null;
        if (tx === null) {
          try { tx = window.prompt("Texto del dibujo:"); } catch (e) {}
        }
        obj.text = tx || "nota";
        var mode = (extra && extra.mode) || "fijo";
        if (mode === "flotante") {
          obj.pinned = false;
          obj.fx = (extra && typeof extra.fx === "number") ? extra.fx : 0.5;
          obj.fy = (extra && typeof extra.fy === "number") ? extra.fy : 0.3;
        } else {
          obj.pinned = true;
        }
      }
      if (type === "fib") obj.levels = fibLevels.slice();
      if (type === "fibext") obj.levels = FIB_EXT.slice();
      return obj;
    }
    function finishObject(type, pts, extra) {
      var obj = makeDrawing(type, pts, extra);
      if (type === "alertline") addAlert("line", pts[0].p);
      pushUndo();
      drawings.push(obj);
      selectedId = obj.id;
      persistDrawings(); redrawDrawings(); paintObjects();
      showSelectedObjectConfig(obj);
      try { U.showToast("ok", type + " creado (" + pts.length + " pts). Herramienta sigue activa; Esc para salir."); } catch (e) {}
    }
    /* Fase 3: conmutar navegación del motor sin duplicar los literales de
     * setTool (misma tabla handleScroll/handleScale nativa de la Fase 2). */
    function setNavEnabled(on) {
      try {
        if (chart && chart.applyOptions) {
          if (!on) chart.applyOptions({ handleScroll: false, handleScale: false });
          else chart.applyOptions({ handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true }, handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true } });
        }
      } catch (e) {}
    }
    /* Un único punto para abortar gestos de dibujo a medias (creación
     * dragState y edición editDrag) y devolver la navegación al motor. Se
     * llama desde loadAll, applyLayout, cleanup y Escape: sin esta limpieza,
     * cambiar de mercado/TF/proveedor o de layout en mitad de un arrastre
     * dejaba estado obsoleto y el siguiente mousemove movía un dibujo del
     * mercado anterior o fallaba. No restaura puntos: esos caminos descartan
     * el contexto entero (Escape sí restaura, vía cancelEditDragApply, justo
     * antes de llamar aquí).
     * Invariante herramienta activa ⇒ navegación desactivada (Fase 4): si
     * hay herramienta pulsada (p. ej. se cambia de TF con Tendencia armada),
     * la navegación SIGUE desactivada. Se eligió condicionar la navegación
     * en vez de setTool(null) porque desarmar la herramienta que el usuario
     * eligió sería peor UX; los botones conservan su aria-pressed tras
     * loadAll/applyLayout, así que deben seguir mandando. Al recargar la
     * página el Estudio nace sin herramienta (activeTool=null), luego ese
     * caso reactiva la navegación como siempre. */
    function cancelDrawingInteractions() {
      dragState = null;
      editDrag = null;
      setNavEnabled(!activeTool);
    }
    function onChartMouseDown(ev) {
      if (!priceSeries || !candles.length) return;
      if (ev.button !== undefined && ev.button !== 0) return;
      var rD = chartBox.getBoundingClientRect();
      var xD = ev.clientX - rD.left, yD = ev.clientY - rD.top;
      if (activeTool) {
        if (activeTool === "freehand") {
          dragState = { tool: "freehand", x0: xD, y0: yD, curX: xD, curY: yD, moved: false, px: [{ x: xD, y: yD }] };
          try { ev.preventDefault(); } catch (e) {}
          return;
        }
        if (DRAG_TOOLS_2[activeTool] || (DRAG_TOOLS_3[activeTool] && pendingPts.length === 0)) {
          var pt = pointFromEvent(ev);
          dragState = { tool: activeTool, x0: pt.x, y0: pt.y, curX: pt.x, curY: pt.y, a: { t: pt.t, p: pt.p }, b: null, moved: false };
          try { ev.preventDefault(); } catch (e) {}
        }
        return;
      }
      /* Sin herramienta: el arrastre SOBRE un objeto lo mueve (cuerpo) o
       * mueve un extremo (punto de control); en zona vacía no se hace nada
       * y el motor navega con su pan nativo. Decisión UX: el objeto tiene
       * prioridad sobre la navegación solo donde hay algo agarrado. */
      var convD = svgConv();
      for (var hi = drawings.length - 1; hi >= 0; hi--) {
        if (drawings[hi].hidden) continue;
        var ht = new DrawingPrimitive(drawings[hi]).hitTest(xD, yD, convD);
        if (ht) {
          var target = drawings[hi];
          selectedId = target.id;
          redrawDrawings(); paintObjects();
          if (canDragDrawing(target)) {
            /* Sin pushUndo aquí: el paso de undo se guarda al primer
             * movimiento real (onChartMouseMove, push diferido). Así un clic
             * simple sobre el objeto no deja un Ctrl+Z fantasma ni borra el
             * redo; al cancelar con Escape se retira ese paso (ver
             * cancelEditDragApply). */
            editDrag = {
              id: target.id, part: ht.part, x0: xD, y0: yD,
              t0: convD.tAt(xD), p0: convD.pAt(yD),
              orig: JSON.parse(JSON.stringify(target.pts)),
              fx0: target.fx, fy0: target.fy, moved: false, redoBackup: null
            };
            setNavEnabled(false); /* el arrastre mueve el objeto, no el gráfico */
            try { ev.preventDefault(); } catch (e) {}
          }
          return;
        }
      }
    }
    function paintFreehandPreview() {
      if (!dragState || dragState.tool !== "freehand" || dragState.px.length < 2) return;
      redrawDrawings();
      var fpts = dragState.px.map(function (q) { return q.x.toFixed(1) + "," + q.y.toFixed(1); }).join(" ");
      try { drawSvg.innerHTML += '<polyline points="' + fpts + '" fill="none" stroke="#4da3ff" stroke-width="2" stroke-dasharray="6 3" stroke-linecap="round" stroke-linejoin="round"/>'; } catch (e) {}
    }
    function onChartMouseMove(ev) {
      /* M1: el crosshair trae el tiempo pero no el píxel; se guarda aquí
       * para colocar #st-marker-tip (ver updateMarkerTip). Va primero para
       * que funcione también sin gesto de dibujo en curso. */
      try { lastMouse = { x: ev.clientX, y: ev.clientY }; } catch (e) {}
      if (editDrag) {
        var rE = chartBox.getBoundingClientRect();
        var cxE = ev.clientX - rE.left, cyE = ev.clientY - rE.top;
        if (!editDrag.moved && Math.hypot(cxE - editDrag.x0, cyE - editDrag.y0) < DRAG_MIN_PX) return;
        var objE = drawings.filter(function (o) { return o.id === editDrag.id; })[0];
        if (!objE) { editDrag = null; setNavEnabled(!activeTool); return; }
        if (!editDrag.moved) {
          /* Push diferido: el undo se guarda al primer movimiento real, no
           * al agarrar (ver cancelEditDragApply para la retirada al
           * cancelar). */
          try { editDrag.redoBackup = redoStack.slice(); } catch (e) { editDrag.redoBackup = null; }
          pushUndo(); /* el arrastre queda cubierto por Ctrl+Z */
          editDrag.moved = true;
        }
        var convE = svgConv();
        if (objE.type === "text" && objE.pinned === false) {
          /* Texto flotante: se mueve en fracción de pantalla, no en pts. */
          var WE = rE.width || 600, HE = chartBox.clientHeight || 380;
          objE.fx = Math.max(0, Math.min(1, num(editDrag.fx0, 0.5) + (cxE - editDrag.x0) / WE));
          objE.fy = Math.max(0, Math.min(1, num(editDrag.fy0, 0.3) + (cyE - editDrag.y0) / HE));
        } else if (editDrag.part >= 0 && editDrag.part < objE.pts.length) {
          /* Un solo extremo, con imán si está activado. */
          objE.pts = moveEndpoint(objE.pts, editDrag.part, convE.tAt(cxE), magnetize(convE.pAt(cyE)));
        } else {
          /* Cuerpo completo: conserva la forma (sin imán, lo deformaría). */
          var dtE = num(convE.tAt(cxE)) - num(editDrag.t0);
          var dpE = num(convE.pAt(cyE)) - num(editDrag.p0);
          objE.pts = movePts(editDrag.orig, dtE, dpE);
        }
        redrawDrawings();
        return;
      }
      if (dragState && dragState.tool === "freehand") {
        var rF = chartBox.getBoundingClientRect();
        var fx = ev.clientX - rF.left, fy = ev.clientY - rF.top;
        dragState.curX = fx; dragState.curY = fy;
        var last = dragState.px[dragState.px.length - 1];
        if (Math.hypot(fx - last.x, fy - last.y) >= FREEHAND_MIN_PX) {
          dragState.px.push({ x: fx, y: fy });
          dragState.moved = true;
          paintFreehandPreview();
        }
        return;
      }
      if (!dragState) return;
      var r = chartBox.getBoundingClientRect();
      dragState.curX = ev.clientX - r.left;
      dragState.curY = ev.clientY - r.top;
      if (Math.hypot(dragState.curX - dragState.x0, dragState.curY - dragState.y0) >= DRAG_MIN_PX) {
        dragState.moved = true;
        paintPreview();
      }
    }
    function onChartMouseUp(ev) {
      if (editDrag) {
        var ed = editDrag;
        editDrag = null;
        setNavEnabled(!activeTool); /* invariante: con herramienta, sigue off */
        suppressChartClick = true; /* el clic tras soltar no reselecciona */
        var objEd = drawings.filter(function (o) { return o.id === ed.id; })[0];
        if (!ed.moved) {
          /* Clic simple sobre el objeto: mismo panel que antes. */
          if (objEd) {
            var rC = chartBox.getBoundingClientRect();
            showFloatingConfig(objEd, ev.clientX - rC.left, ev.clientY - rC.top);
          }
          redrawDrawings(); paintObjects();
        } else {
          if (objEd) {
            selectedId = objEd.id;
            persistDrawings(); redrawDrawings(); paintObjects();
            showSelectedObjectConfig(objEd);
          } else {
            persistDrawings(); redrawDrawings(); paintObjects();
          }
        }
        return;
      }
      if (dragState && dragState.tool === "freehand") {
        var fh = dragState;
        dragState = null;
        if (!fh.moved || fh.px.length < 2) { redrawDrawings(); return; }
        /* Sin imán a propósito: magnetizar cada punto lo escalonaría a
         * OHLC y destruiría el gesto. */
        var kept = decimateStroke(fh.px, FREEHAND_MIN_PX);
        var convF = svgConv();
        finishObject("freehand", strokeToTimePrice(kept, convF));
        showHint(TOOL_HINTS.freehand || "");
        return;
      }
      if (!dragState) return;
      var st = dragState;
      dragState = null;
      var r = chartBox.getBoundingClientRect();
      var curX = ev.clientX - r.left, curY = ev.clientY - r.top;
      if (Math.hypot(curX - st.x0, curY - st.y0) < DRAG_MIN_PX) {
        redrawDrawings();
        return; /* clic simple sin arrastre: no crea basura */
      }
      var conv = svgConv();
      var A = st.a, B = { t: conv.tAt(curX), p: magnetize(conv.pAt(curY)) };
      if (DRAG_TOOLS_2[st.tool]) {
        finishObject(st.tool, [A, B]); /* respeta orden A→B del usuario */
        /* herramienta se mantiene activa para dibujar varias; Esc cancela */
        showHint(TOOL_HINTS[st.tool] || "");
      } else if (DRAG_TOOLS_3[st.tool]) {
        pendingPts = [A, B];
        redrawDrawings();
        showHint(st.tool === "fibext" ? "Ahora clic para el punto C de la extensión." : "Ahora clic para el target (tercer punto).");
      }
    }
    function onChartMouseLeave() {
      /* M1: al salir el puntero se oculta el tooltip PAPER (el crosshair
       * también lo oculta vía param vacío, pero el mouseleave es
       * determinista y cubre salidas rápidas sin evento del motor). */
      lastMouse = null;
      try { hideMarkerTip(); } catch (e) {}
      if (editDrag) {
        /* El puntero salió en mitad del arrastre: se consolida lo movido
         * (persistir + reactivar navegación) en vez de dejarla desactivada.
         * Decisión documentada (Fase 4, ESTADO_Y_FALLOS_PENDIENTES): al salir
         * del lienzo no hay más mousemove, así que el gesto no puede
         * continuar; cancelar tiraría el trabajo del usuario y dejaría un
         * paso de undo diferido huérfano. Se conserva la invariante de
         * herramienta (navegación off si hay herramienta pulsada). */
        editDrag = null;
        setNavEnabled(!activeTool);
        persistDrawings(); redrawDrawings(); paintObjects();
      }
      if (dragState && dragState.tool === "freehand" && dragState.moved && dragState.px.length >= 2) {
        var fhL = dragState;
        dragState = null;
        var convL = svgConv();
        finishObject("freehand", strokeToTimePrice(decimateStroke(fhL.px, FREEHAND_MIN_PX), convL));
        showHint(TOOL_HINTS.freehand || "");
        return;
      }
      if (dragState && !dragState.moved) dragState = null;
    }
    function onChartClick(ev) {
      if (!priceSeries || !candles.length) return;
      if (suppressChartClick) { suppressChartClick = false; return; }
      var r = chartBox.getBoundingClientRect();
      var x = ev.clientX - r.left, y = ev.clientY - r.top;
      var conv = svgConv();
      var t = conv.tAt(x), p = magnetize(conv.pAt(y));
      /* Si hay herramienta activa, delegar al comportamiento de dibujo */
      if (activeTool) {
        if (DRAG_TOOLS_2[activeTool] || activeTool === "freehand") return; /* se crea solo por arrastre (mouseup) */
        if (DRAG_TOOLS_3[activeTool]) {
          if (pendingPts.length === 2) {
            var pt = pointFromEvent(ev);
            var third = { t: pt.t, p: pt.p };
            var all = [pendingPts[0], pendingPts[1], third];
            pendingPts = [];
            finishObject(activeTool, all);
            showHint(TOOL_HINTS[activeTool] || "");
          }
          return; /* los 2 primeros puntos llegan por arrastre */
        }
        if (activeTool === "text") {
          var fx = r.width ? x / r.width : 0.5;
          var fy = (chartBox.clientHeight || 380) ? y / (chartBox.clientHeight || 380) : 0.3;
          pendingPts = [];
          finishObject(activeTool, [{ t: t, p: p }], { mode: "fijo", fx: fx, fy: fy });
          setTool(null);
          return;
        }
        pendingPts.push({ t: t, p: p });
        var need = 1;
        if (pendingPts.length >= need) {
          var pts = pendingPts.slice();
          pendingPts = [];
          finishObject(activeTool, pts);
          if (activeTool === "h" || activeTool === "v" || activeTool === "text" || activeTool === "alertline") setTool(null);
        }
        return;
      }
      /* Sin herramienta activa: click selecciona dibujo cercano (hitTest) */
      var hit = null, hitObj = null;
      for (var i = drawings.length - 1; i >= 0; i--) {
        if (drawings[i].hidden) continue;
        var d = new DrawingPrimitive(drawings[i]);
        var ht = d.hitTest(x, y, conv);
        if (ht) { hit = ht; hitObj = drawings[i]; break; }
      }
      if (hitObj) {
        selectedId = hitObj.id;
        showFloatingConfig(hitObj, x, y);
      } else {
        selectedId = null;
        hideFloatingConfig();
      }
      redrawDrawings(); paintObjects();
    }

    /* --- Panel flotante "Configurar" junto al objeto seleccionado (CAMBIO A) --- */
    var floatPanel = null;
    function showSelectedObjectConfig(obj) {
      if (!obj || obj.id !== selectedId) { hideFloatingConfig(); return; }
      var r = chartBox.getBoundingClientRect();
      showFloatingConfig(obj, Math.max(12, r.width - 286), 12);
    }
    function showFloatingConfig(obj, cx, cy) {
      hideFloatingConfig();
      if (!obj || !selectedId || obj.id !== selectedId) return;
      var panel = document.createElement("div");
      panel.className = "float-config";
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-label", "Configurar " + obj.type);
      var html = '<button class="float-close" aria-label="Cerrar">×</button><h4>Configurar · ' + U.esc(obj.type.toUpperCase()) + '</h4>';
      var type = obj.type;
      if (type === "fib" || type === "fibext") {
        html += '<div class="fc-row"><strong>Niveles Fibonacci</strong></div>';
        var lvls = (type === "fib" ? fibLevels : FIB_EXT);
        var objLevels = Array.isArray(obj.levels) && obj.levels.length ? obj.levels : lvls;
        var all = (type === "fib" ? FIB_DEFAULT : FIB_EXT).concat(objLevels.filter(function (v) {
          var base = type === "fib" ? FIB_DEFAULT : FIB_EXT;
          return base.indexOf(Number(v)) < 0;
        }));
        all = all.map(Number).filter(isFinite).sort(function (a, b) { return a - b; });
        all.forEach(function (L) {
          var on = objLevels.indexOf(L) >= 0;
          html += '<label class="fc-chk"><input type="checkbox" data-fib="' + U.esc(String(L)) + '"' + (on ? " checked" : "") + '> ' + U.esc(String(L)) + '</label>';
        });
        html += '<div class="fc-row"><input type="text" id="fc-fibadd" placeholder="custom (ej. 1.272)" inputmode="decimal"><button class="ghost" id="fc-fibpush">Añadir</button></div>';
      }
      if (type === "text") {
        html += '<div class="fc-row"><label>Texto <input type="text" id="fc-txt" value="' + U.esc(obj.text || "") + '"></label></div>';
        html += '<div class="fc-row"><label><select id="fc-pin"><option value="fijo"' + (obj.pinned !== false ? " selected" : "") + '>Fijo (anclado)</option><option value="flotante"' + (obj.pinned === false ? " selected" : "") + '>Flotante (pantalla %)</option></select></label></div>';
      }
      var safeColor = isValidHexColor(obj.color) ? obj.color : (type === "alertline" ? "#d29922" : "#e3b341");
      html += '<div class="fc-row"><label>Color <input type="color" id="fc-col" value="' + U.esc(safeColor) + '"></label></div>';
      /* Fase 3: grosor y estilo (sin sentido en texto, que no tiene trazo). */
      if (type !== "text") {
        var curW = effWidth(obj, 1.5);
        html += '<div class="fc-row"><label>Grosor <select id="fc-width">' +
          DRAW_WIDTHS.map(function (w) {
            var nm = w === 1 ? "Fino" : (w === 1.5 ? "Normal" : (w === 2.5 ? "Grueso" : "Muy grueso"));
            return '<option value="' + w + '"' + (curW === w ? " selected" : "") + ">" + nm + " (" + w + " px)</option>";
          }).join("") + "</select></label></div>";
        var curStyle = obj.style === "dashed" ? "dashed" : (obj.style === "solid" ? "solid" : ((typeof obj.dash === "string" && obj.dash) ? "dashed" : "solid"));
        html += '<div class="fc-row"><label>Estilo <select id="fc-style"><option value="solid"' + (curStyle === "solid" ? " selected" : "") + '>Sólida</option><option value="dashed"' + (curStyle === "dashed" ? " selected" : "") + '>Discontinua</option></select></label></div>';
      }
      var curOp = Math.round(effOpacity(obj) * 100);
      html += '<div class="fc-row"><label>Opacidad <input type="range" id="fc-op" min="0" max="100" step="5" value="' + curOp + '"> <span id="fc-opv">' + curOp + " %</span></label></div>";
      html += '<div class="fc-row"><label class="kbd"><input type="checkbox" id="fc-lock"' + (obj.locked ? " checked" : "") + '> Bloquear</label></div>';
      html += '<div class="fc-row"><label class="kbd"><input type="checkbox" id="fc-hide"' + (obj.hidden ? " checked" : "") + '> Ocultar</label></div>';
      html += '<div class="fc-row"><button class="ghost" id="fc-dup">Duplicar</button></div>';
      html += '<div class="fc-row"><button class="ghost danger" id="fc-del">Eliminar</button></div>';
      panel.innerHTML = html;
      /* Posicionar junto al cursor/objeto */
      var r = chartBox.getBoundingClientRect();
      panel.style.left = Math.min(cx + 16, r.width - 260) + "px";
      panel.style.top = Math.min(cy + 16, r.height - 300) + "px";
      chartBox.appendChild(panel);
      floatPanel = panel;
      /* Eventos */
      panel.querySelector(".float-close").addEventListener("click", hideFloatingConfig);
      if (type === "fib" || type === "fibext") {
        panel.querySelectorAll("[data-fib]").forEach(function (cb) {
          cb.addEventListener("change", function () {
            var v = Number(cb.getAttribute("data-fib"));
            if (!obj.levels) obj.levels = (type === "fib" ? fibLevels : FIB_EXT).slice();
            if (cb.checked && obj.levels.indexOf(v) < 0) obj.levels.push(v);
            if (!cb.checked) obj.levels = obj.levels.filter(function (x) { return Number(x) !== v; });
            obj.levels.sort(function (a, b) { return a - b; });
            persistDrawings(); redrawDrawings(); paintObjects();
            showFloatingConfig(obj, cx, cy); /* refrescar panel */
          });
        });
        panel.querySelector("#fc-fibpush").addEventListener("click", function () {
          var v = Number((panel.querySelector("#fc-fibadd").value || "").replace(",", "."));
          if (!isFinite(v)) { U.showToast("blocked", "Escribe un nivel válido."); return; }
          if (!obj.levels) obj.levels = (type === "fib" ? fibLevels : FIB_EXT).slice();
          if (obj.levels.indexOf(v) < 0) obj.levels.push(v);
          obj.levels.sort(function (a, b) { return a - b; });
          persistDrawings(); redrawDrawings(); paintObjects();
          showFloatingConfig(obj, cx, cy);
        });
      }
      if (type === "text") {
        panel.querySelector("#fc-txt").addEventListener("change", function () {
          pushUndo(); obj.text = this.value; persistDrawings(); redrawDrawings(); paintObjects();
        });
        panel.querySelector("#fc-pin").addEventListener("change", function () {
          pushUndo();
          if (this.value === "flotante") {
            obj.pinned = false;
            if (typeof obj.fx !== "number" || typeof obj.fy !== "number") {
              var rr = chartBox.getBoundingClientRect();
              var conv = svgConv();
              var q = obj.pts[0] || { t: 0, p: 0 };
              obj.fx = rr.width ? conv.tx(q.t) / rr.width : 0.5;
              obj.fy = (chartBox.clientHeight || 380) ? conv.py(q.p) / (chartBox.clientHeight || 380) : 0.3;
            }
          } else {
            obj.pinned = true;
          }
          persistDrawings(); redrawDrawings(); paintObjects();
          showFloatingConfig(obj, cx, cy);
        });
      }
      panel.querySelector("#fc-col").addEventListener("change", function () {
        pushUndo(); obj.color = this.value; persistDrawings(); redrawDrawings(); paintObjects();
      });
      var wSel = panel.querySelector("#fc-width");
      if (wSel) wSel.addEventListener("change", function () {
        pushUndo(); obj.width = num(this.value, 1.5); persistDrawings(); redrawDrawings(); paintObjects();
      });
      var stSel = panel.querySelector("#fc-style");
      if (stSel) stSel.addEventListener("change", function () {
        pushUndo();
        obj.style = this.value;
        try { delete obj.dash; } catch (e) { obj.dash = ""; } /* el estilo absorbe al dash legado */
        persistDrawings(); redrawDrawings(); paintObjects();
      });
      var opRange = panel.querySelector("#fc-op");
      if (opRange) {
        var opUndone = false;
        opRange.addEventListener("input", function () {
          if (!opUndone) { pushUndo(); opUndone = true; } /* el gesto entero es un solo paso de Ctrl+Z */
          obj.opacity = num(this.value, 100) / 100;
          var lab = panel.querySelector("#fc-opv");
          if (lab) lab.textContent = this.value + " %";
          redrawDrawings();
        });
        opRange.addEventListener("change", function () {
          obj.opacity = num(this.value, 100) / 100;
          persistDrawings(); redrawDrawings(); paintObjects();
        });
      }
      panel.querySelector("#fc-dup").addEventListener("click", function () {
        pushUndo();
        /* Desplazamiento pequeño y visible: 3 velas en tiempo, 0.5 % en precio. */
        var step = 60;
        try { if (candles.length > 1) step = Math.abs(num(candles[1].time) - num(candles[0].time)) || 60; } catch (e) {}
        var ref = 0;
        try { if (candles.length) ref = num(candles[candles.length - 1].close); } catch (e2) {}
        var nid = "d" + Date.now().toString(36) + Math.floor(Math.random() * 1e4);
        var copy = dupDrawing(obj, 3 * step, ref ? Math.abs(ref) * 0.005 : 1, nid);
        drawings.push(copy);
        selectedId = nid;
        persistDrawings(); redrawDrawings(); paintObjects();
        showFloatingConfig(copy, cx, cy);
      });
      panel.querySelector("#fc-lock").addEventListener("change", function () {
        pushUndo(); obj.locked = this.checked; persistDrawings(); redrawDrawings(); paintObjects();
      });
      panel.querySelector("#fc-hide").addEventListener("change", function () {
        pushUndo(); obj.hidden = this.checked; persistDrawings(); redrawDrawings(); paintObjects();
      });
      panel.querySelector("#fc-del").addEventListener("click", function () {
        pushUndo(); drawings = removeDrawing(drawings, obj.id); selectedId = null; hideFloatingConfig(); persistDrawings(); redrawDrawings(); paintObjects();
      });
    }
    function hideFloatingConfig() {
      if (floatPanel && floatPanel.parentNode) floatPanel.parentNode.removeChild(floatPanel);
      floatPanel = null;
    }
    function onContextMenu(ev) {
      ev.preventDefault();
      tip.innerHTML = '<button type="button" data-c="del">Eliminar selección</button>' +
        '<button type="button" data-c="lock">Bloquear/desbloquear</button>' +
        '<button type="button" data-c="hide">Ocultar/mostrar</button>' +
        '<button type="button" data-c="alert">Alerta desde aquí</button>';
      tip.hidden = false;
      tip.style.left = ev.clientX + "px";
      tip.style.top = ev.clientY + "px";
      tip.querySelectorAll("button").forEach(function (b) {
        b.addEventListener("click", function () {
          var c = b.getAttribute("data-c");
          var d = drawings.filter(function (o) { return o.id === selectedId; })[0];
          if (c === "del" && d && !d.locked) { pushUndo(); drawings = removeDrawing(drawings, selectedId); selectedId = null; }
          if (c === "lock" && d) d.locked = !d.locked;
          if (c === "hide" && d) d.hidden = !d.hidden;
          if (c === "alert" && priceSeries) {
            var r = chartBox.getBoundingClientRect();
            var conv = svgConv();
            addAlert("line", conv.pAt(ev.clientY - r.top));
          }
          tip.hidden = true;
          persistDrawings(); redrawDrawings(); paintObjects();
        });
      });
    }
    /* Zoom +/-/Centrar sobre el rango lógico del motor real (se conserva:
     * la rueda nativa ya centra en el puntero; estos botones centran en el
     * medio con el mismo mecanismo). Los literales 0.75/1.33 son contrato.
     * Degrada honesto: si la API no está, no rompe ni inventa datos. */
    function zoomBy(factor) {
      if (!chart || !candles.length) return;
      try {
        var ts = chart.timeScale();
        var r = null;
        try { r = ts.getVisibleLogicalRange(); } catch (e) { r = null; }
        if (!r) {
          var n = Math.max(10, candles.length);
          r = { from: 0, to: n - 1 };
        }
        var mid = (num(r.from) + num(r.to)) / 2;
        var span = Math.max(5, (num(r.to) - num(r.from)) * factor);
        ts.setVisibleLogicalRange({ from: mid - span / 2, to: mid + span / 2 });
      } catch (e) { /* motor ausente: honesto, sin romper */ }
      redrawDrawings();
    }
    /* Convierte coordenada x en tiempo usando solo API pública v5.
     * Devuelve timestamp (segundos) o null si no se puede determinar. */
    function coordinateToTimePublic(x) {
      var ts = chart && chart.timeScale ? chart.timeScale() : null;
      if (ts && typeof ts.coordinateToTime === "function") {
        try {
          var t = ts.coordinateToTime(x);
          if (typeof t === "number" && isFinite(t)) return t;
        } catch (e) {}
      }
      return null; /* fallback honesto: llamador decide qué hacer */
    }
    function setTool(t) {
      activeTool = t; pendingPts = []; dragState = null;
      root.querySelectorAll("[data-tool]").forEach(function (b) {
        b.setAttribute("aria-pressed", b.getAttribute("data-tool") === t ? "true" : "false");
      });
      try {
        if (t) chartBox.classList.add("st-tool-active");
        else chartBox.classList.remove("st-tool-active");
      } catch (e) {}
      /* Con herramienta activa el arrastre dibuja: se desactiva el scroll/
       * pan/zoom del motor para no competir; sin herramienta, navegación
       * nativa completa (Fase 2: rueda con zoom anclado, arrastre, eje). */
      setNavEnabled(!t);
      if (t && TOOL_HINTS[t]) showHint(TOOL_HINTS[t]);
      else if (!t) showHint(null);
    }

    /* ---------------- preferencia compacta de la barra de dibujo ----------------
     * Posición [izquierda|derecha|arriba|abajo], por defecto izquierda
     * vertical; en móvil (<=768px) empieza abajo. La toolbar .drawtools queda
     * solo con iconos de dibujo; el único selector vive en el details compacto
     * Preferencias de herramientas dentro de Estudio.
     * Persistente en localStorage orbis.estudio.toolsdock + /ux/state
     * kind=toolsdock. Se migra sin perder el valor: misma clave, sin reset. */
    var DOCK_KEY = "orbis.estudio.toolsdock";
    function dockDefault() {
      var stored = readDockStored();
      if (stored) return stored;
      try {
        if (window.innerWidth && window.innerWidth <= 768) return "abajo";
      } catch (e) {}
      return "izquierda";
    }
    var DOCK_CLS = { izquierda: "dock-left", derecha: "dock-right", arriba: "dock-top", abajo: "dock-bottom" };
    /* Lee la config global (misma clave que #/config): no resetea nunca un
     * valor válido ya guardado (migración sin pérdida). */
    function readDockStored() {
      try {
        var s = localStorage.getItem(DOCK_KEY);
        if (DOCK_OPTS.indexOf(s) >= 0) return s;
      } catch (e) {}
      return null;
    }
    function applyDock(pos, skipPersist) {
      if (DOCK_OPTS.indexOf(pos) < 0) pos = "izquierda";
      var wrap = $("st-dockwrap"), bar = $("st-drawtools"), sel = $("st-tools-dock");
      if (wrap) wrap.className = "stchartdock " + DOCK_CLS[pos];
      if (bar) bar.className = "drawtools " + DOCK_CLS[pos];
      if (sel && sel.value !== pos) sel.value = pos;
      if (!skipPersist) {
        try { localStorage.setItem(DOCK_KEY, pos); } catch (e) {}
        U.api("/ux/state", { method: "PUT", body: JSON.stringify({ market: "__dock", provider: "all", timeframe: "x", kind: "toolsdock", payload: { pos: pos } }) }).catch(function () {});
      }
    }
    /* Sincronizar la preferencia entre pestañas. */
    if (!window._orbisDockStorageArmed) {
      window._orbisDockStorageArmed = true;
      try {
        var _orbisDockStorageHandler = function (ev) {
          if (ev && ev.key === DOCK_KEY && DOCK_OPTS.indexOf(ev.newValue) >= 0) {
            applyDock(ev.newValue, true);
          }
        };
        window.addEventListener("storage", _orbisDockStorageHandler);
        window._orbisDockStorageHandler = _orbisDockStorageHandler;
      } catch (e) {}
    }
    function loadDockFromServer() {
      U.api("/ux/state?market=" + encodeURIComponent("__dock") + "&provider=all&timeframe=x&kind=toolsdock").then(function (r) {
        var pos = r && r.data && r.data.payload && r.data.payload.pos;
        if (DOCK_OPTS.indexOf(pos) < 0) return;
        try {
          if (localStorage.getItem(DOCK_KEY)) return; /* el local manda */
        } catch (e) {}
        applyDock(pos, true);
        try { localStorage.setItem(DOCK_KEY, pos); } catch (e2) {}
      }).catch(function () {});
    }

    function paintObjects() {
      var box = $("st-objects");
      if (!box) return;
      if (!drawings.length) { box.innerHTML = '<p class="muted">Sin dibujos en ' + U.esc(market) + ".</p>"; return; }
      var html = '<ul class="wl">';
      drawings.forEach(function (d) {
        html += '<li><span><strong>' + U.esc(d.type) + "</strong>" + (d.locked ? " 🔒" : "") + (d.hidden ? " (oculto)" : "") + "</span>" +
          '<span><button type="button" class="ghost" data-sel="' + d.id + '">Configurar</button> ' +
          '<button type="button" class="ghost" data-del="' + d.id + '">Eliminar</button></span></li>';
      });
      box.innerHTML = html + "</ul>";
      box.querySelectorAll("[data-sel]").forEach(function (b) {
        b.addEventListener("click", function () {
          selectedId = b.getAttribute("data-sel");
          var d = drawings.filter(function (o) { return o.id === selectedId; })[0];
          redrawDrawings(); paintObjects(); showSelectedObjectConfig(d);
        });
      });
      box.querySelectorAll("[data-del]").forEach(function (b) {
        b.addEventListener("click", function () {
          pushUndo();
          var deleting = b.getAttribute("data-del");
          drawings = removeDrawing(drawings, deleting);
          if (selectedId === deleting) { selectedId = null; hideFloatingConfig(); }
          persistDrawings(); redrawDrawings(); paintObjects();
        });
      });
    }

    /* ---------------- alertas ---------------- */
    function addAlert(op, price) {
      var threshold = String(price);
      var source = provider === "auto" && chartResolvedProvider ? chartResolvedProvider : provider;
      var condition = op === "line" ? (lastPrice !== null && Number(price) < lastPrice ? "<" : ">") :
        op === "lt" ? "<" : ">";
      window.OrbisMarketAlerts.add(market, source, condition, threshold).then(function () {
        U.showToast("ok", market + " · " + condition + " " + threshold + " · Alerta creada.");
      }).catch(function (error) { U.showToast("blocked", error.message); });
    }
    var previousAlertsMarkup;
    function paintAlerts() {
      var box = $("st-alerts");
      if (!box) return;
      var state = window.OrbisMarketAlerts.state();
      var rules = state.rules.filter(function (rule) { return rule.symbol === market; });
      var markup = state.storageError ? '<p role="alert">Alertas locales no disponibles.</p>' :
        rules.length ? '<ul class="wl">' + rules.map(function (a) {
          return "<li><span>" + U.esc(a.symbol + " " + a.op + " " + a.price) + "</span>" +
            '<button type="button" class="ghost" data-remove-alert="' + U.esc(a.id) + '">Quitar</button></li>';
        }).join("") + "</ul>" : '<p class="muted">Sin alertas para este mercado.</p>';
      if (markup !== previousAlertsMarkup) { box.innerHTML = markup; previousAlertsMarkup = markup; }
    }
    var stopAlertUpdates = window.OrbisMarketAlerts.onChange(paintAlerts);
    $("st-alerts").addEventListener("click", function (event) {
      var button = event.target.closest("[data-remove-alert]");
      if (button) window.OrbisMarketAlerts.remove(button.dataset.removeAlert).catch(function (error) {
        U.showToast("blocked", error.message);
      });
    });

    /* ---------------- indicadores UI / watchlist / buscador ---------------- */
    function paintInds() {
      var box = $("st-inds");
      if (!box) return;
      function row(key, label, extra) {
        var c = indCfg[key];
        return '<label class="kbd"><input type="checkbox" data-ind="' + key + '"' + (c.on ? " checked" : "") + "> " + label + "</label> " + (extra || "");
      }
      box.innerHTML =
        "<div>" + row("sma", "SMA sobre precio", ' periodo <input id="ind-sma-p" value="' + U.esc(indCfg.sma.p) + '" size="4">') + "</div>" +
        "<div>" + row("ema", "EMA sobre precio", ' periodos <input id="ind-ema-p" value="' + U.esc(indCfg.ema.p) + '" size="12" title="Uno o varios periodos separados por comas, p. ej. 9,20,50">') + "</div>" +
        "<div>" + row("bb", "Bollinger", ' periodo <input id="ind-bb-p" value="' + U.esc(indCfg.bb.p) + '" size="4"> mult <input id="ind-bb-m" value="' + U.esc(indCfg.bb.m) + '" size="3">') + "</div>" +
        "<div>" + row("rsi", "RSI panel propio", ' periodo <input id="ind-rsi-p" value="' + U.esc(indCfg.rsi.p) + '" size="4">') + "</div>" +
        "<div>" + row("macd", "MACD panel propio", ' rápida <input id="ind-macd-f" value="' + U.esc(indCfg.macd.f) + '" size="3"> lenta <input id="ind-macd-s" value="' + U.esc(indCfg.macd.s) + '" size="3"> señal <input id="ind-macd-sg" value="' + U.esc(indCfg.macd.sig) + '" size="3">') + "</div>";
      box.querySelectorAll("[data-ind]").forEach(function (cb) {
        cb.addEventListener("change", function () {
          var k = cb.getAttribute("data-ind");
          indCfg[k].on = cb.checked;
          persistInd(); paintIndicators(); maybeRefillForIndicators();
        });
      });
      [["ind-sma-p", "sma", "p"], ["ind-ema-p", "ema", "p"], ["ind-bb-p", "bb", "p"], ["ind-bb-m", "bb", "m"],
       ["ind-rsi-p", "rsi", "p"], ["ind-macd-f", "macd", "f"], ["ind-macd-s", "macd", "s"], ["ind-macd-sg", "macd", "sig"]].forEach(function (m) {
        var el = root.querySelector("#" + m[0]);
        if (el) el.addEventListener("change", function () {
          /* ema.p admite lista ("9,20,50") y se guarda tal cual para no
           * romper la persistencia; el resto siguen siendo numéricos. */
          if (m[0] === "ind-ema-p") {
            var raw = String(el.value || "").trim();
            indCfg.ema.p = raw === "" ? indCfg.ema.p : raw;
          } else {
            indCfg[m[1]][m[2]] = num(el.value, indCfg[m[1]][m[2]]);
          }
          persistInd(); paintIndicators(); maybeRefillForIndicators();
        });
      });
    }
    function paintWatchlist() {
      var box = $("st-wl");
      if (!box) return;
      var local = loadLocal("__wl", "all", "x", "items", []);
      U.api("/ux/watchlist").then(function (r) {
        var items = (r.ok && Array.isArray(r.data) && r.data.length) ? r.data : local;
        if (!items.length) { box.innerHTML = '<li><span class="muted">Todavía no sigues ningún mercado.</span></li>'; return; }
        box.innerHTML = items.map(function (w) {
          var mk = typeof w === "string" ? w : w.market;
          var pv = (w && w.provider) || provider;
          return '<li><span><strong>' + U.esc(mk) + '</strong> <span class="muted">' + U.esc(pv) + '</span></span>' +
            '<span><button type="button" class="ghost" data-open="' + U.esc(mk) + '">Abrir</button> ' +
            '<button type="button" class="ghost" data-wdel="' + U.esc(mk) + '">×</button></span></li>';
        }).join("");
        box.querySelectorAll("[data-open]").forEach(function (b) {
          b.addEventListener("click", function () {
            $("st-pair").value = b.getAttribute("data-open");
            loadAll();
          });
        });
        box.querySelectorAll("[data-wdel]").forEach(function (b) {
          b.addEventListener("click", function () {
            var mk = b.getAttribute("data-wdel");
            var next = items.filter(function (w) { return (typeof w === "string" ? w : w.market) !== mk; });
            saveLocal("__wl", "all", "x", "items", next);
            U.api("/ux/watchlist?market=" + encodeURIComponent(mk), { method: "DELETE" }).catch(function () {});
            paintWatchlist();
          });
        });
      }).catch(function () {
        box.innerHTML = '<li><span class="muted">Sin conexión.</span></li>';
      });
    }
    function armSearch() {
      var input = $("st-pair"), out = $("st-search");
      var t = null, generation = 0;
      function close() {
        out.hidden = true;
        out.innerHTML = "";
        input.setAttribute("aria-expanded", "false");
      }
      function open(html) {
        out.innerHTML = html;
        out.hidden = false;
        input.setAttribute("aria-expanded", "true");
      }
      out.addEventListener("mousedown", function (event) {
        /* No disparar change/blur del input antes de elegir el mercado. */
        if (event.target.closest("button")) event.preventDefault();
      });
      out.addEventListener("click", function (event) {
        var button = event.target.closest("button[data-market]");
        if (!button) return;
        input.value = button.dataset.market;
        $("st-prov").value = button.dataset.provider;
        generation++;
        close();
        loadAll();
      });
      out.addEventListener("keydown", function (event) {
        var buttons = Array.from(out.querySelectorAll("button[data-market]"));
        var index = buttons.indexOf(document.activeElement);
        if (event.key === "Escape") { event.preventDefault(); close(); input.focus(); }
        if (event.key === "ArrowDown" && index < buttons.length - 1) {
          event.preventDefault(); buttons[index + 1].focus();
        }
        if (event.key === "ArrowUp") {
          event.preventDefault();
          if (index <= 0) input.focus(); else buttons[index - 1].focus();
        }
      });
      input.addEventListener("keydown", function (event) {
        if (event.key === "Escape") { generation++; close(); }
        if (event.key === "ArrowDown" && !out.hidden) {
          var first = out.querySelector("button[data-market]");
          if (first) { event.preventDefault(); first.focus(); }
        }
        if (event.key === "Enter") {
          event.preventDefault();
          var option = out.querySelector("button[data-market]");
          if (option) option.click(); else { generation++; close(); loadAll(); }
        }
      });
      input.addEventListener("blur", function () {
        setTimeout(function () {
          if (!input.isConnected) return;
          if (!out.contains(document.activeElement)) { generation++; close(); }
        }, 0);
      });
      $("st-prov").addEventListener("change", function () { generation++; close(); });
      input.addEventListener("input", function () {
        if (t) clearTimeout(t);
        var request = ++generation;
        t = setTimeout(function () {
          if (!input.isConnected) return;
          var q = input.value.trim().toUpperCase();
          if (q.length < 2) { close(); return; }
          var requestedProvider = $("st-prov").value;
          U.api("/market/symbols?provider=" + encodeURIComponent(requestedProvider) + "&search=" + encodeURIComponent(q)).then(function (r) {
            if (!input.isConnected) return;
            if (request !== generation || input.value.trim().toUpperCase() !== q || $("st-prov").value !== requestedProvider) return;
            if (!r.ok || !r.data) { close(); return; }
            var names = [], seen = {};
            Object.keys(r.data.providers || {}).forEach(function (k) {
              (r.data.providers[k] || []).forEach(function (s) {
                var key = s + "|" + k;
                if (names.length < 8 && !seen[key]) { seen[key] = true; names.push({ market: s, provider: k }); }
              });
            });
            if (!names.length) { open('<span class="muted">Sin mercados para esta búsqueda.</span>'); return; }
            open(names.map(function (item) {
              return '<button type="button" data-market="' + U.esc(item.market) + '" data-provider="' + U.esc(item.provider) + '"><strong>' + U.esc(item.market) + '</strong><span>' + U.esc(item.provider.toUpperCase()) + '</span></button>';
            }).join(""));
          }).catch(function () { if (input.isConnected && request === generation) close(); });
        }, 300);
      });
    }

    function loadAll() {
      loadGeneration++;
      loading = false;
      lastHistoryGesture = 0;
      stopStream();
      lastTradeQuote = null; lastTradeQuoteReceivedMs = null;
      pendingTradeRequest = null;
      syncTradePrice();
      chartResolvedProvider = null;
      if ($("st-quality")) { $("st-quality").hidden = true; $("st-quality").textContent = ""; }
      /* Sin estado a medias del mercado anterior: aborta cualquier gesto de
       * dibujo en curso y restaura la navegación (ver Bloqueante 2). */
      cancelDrawingInteractions();
      market = canon2($("st-pair").value || "BTC/EUR");
      provider = $("st-prov").value;
      var modeLine = document.getElementById("modeline");
      if (modeLine) modeLine.textContent = "Mercado público · Fuente del Estudio " + provider.toUpperCase();
      timeframe = $("st-tf").value;
      resetArchiveDisplay();
      paintRefreshNote();
      chartType = $("st-ctype").value;
      try {
        localStorage.setItem(persistenceKey("cfg", "all", "x", "lastmarket"), JSON.stringify(market));
        localStorage.setItem(persistenceKey("cfg", "all", "x", "lasttf"), JSON.stringify(timeframe));
        localStorage.setItem(persistenceKey("cfg", "all", "x", "lastctype"), JSON.stringify(chartType));
        saveLocal("cfg", "all", "x", "provider", $("st-prov").value);
      } catch (e) {}
      if (chart) { try { chart.remove(); } catch (e) {} chart = null; priceSeries = null; volSeries = null; cmpSeries = null; indSeries = {}; indSeriesMeta = {}; priceMarkers = null; paperMarks = []; planMarks = []; paperOps = []; paperBarGroups = Object.create(null); entryPlan = null; entryLine = null; pnlLine = null; /* M3 */ lastMouse = null; try { hideMarkerTip(); } catch (e) {} }
      chartBox.innerHTML = "";
      candles = []; selectedId = null; hasMore = true;
      refreshing = false; refreshLastClose = null; /* el refresco incremental arranca limpio */
      resetScaleMemory(); /* chart nuevo = escala automática de nuevo (R1; incluye priceProgTag) */
      hideFloatingConfig();
      closeTradeForm(); /* M4: el formulario es del mercado anterior */
      var tradeMsgBox = $("st-trade-msg");
      if (tradeMsgBox) tradeMsgBox.innerHTML = "";
      paintTradePair();
      undoStack = []; redoStack = [];
      ensureChart();
      loadPersisted();
      paintObjects(); paintInds(); paintCandles(); paintAlerts(); paintWatchlist();
      loadCandles(false);
      restartEstudioTimer();
    }

    /* ---------------- eventos ---------------- */
    $("st-load").addEventListener("click", function () {
      if (!$("st-range-dates").hidden) {
        $("st-range-error").textContent = "Aplica o cancela las fechas antes de actualizar.";
        $("st-range-error").hidden = false;
        return;
      }
      loadAll();
    });
    root.querySelector(".st-view-actions-menu").addEventListener("click", function (event) {
      if (event.target.closest("button")) root.querySelector(".st-view-actions").open = false;
    });
    function openCustomRange() {
      var lastDate = candles.length ? new Date(candles[candles.length - 1].time * 1000) : new Date();
      var firstDate = candles.length ? new Date(candles[0].time * 1000) :
        new Date(Date.UTC(lastDate.getUTCFullYear() - 1, lastDate.getUTCMonth(), lastDate.getUTCDate()));
      if (!$("st-range-from").value) $("st-range-from").value = firstDate.toISOString().slice(0, 10);
      if (!$("st-range-to").value) $("st-range-to").value = lastDate.toISOString().slice(0, 10);
      $("st-range-error").hidden = true;
      $("st-range-dates").hidden = false;
      $("st-range-edit").hidden = false;
      $("st-range-edit").textContent = appliedRange === "custom" ? "Cambiar fechas" : "Elegir fechas";
      $("st-range-edit").setAttribute("aria-expanded", "true");
      $("st-range-from").focus();
    }
    function closeCustomRange() {
      $("st-range-dates").hidden = true;
      $("st-range-edit").setAttribute("aria-expanded", "false");
    }
    $("st-range").addEventListener("change", function () {
      if (this.value === "custom") {
        // El gráfico puede tener una carga previa todavía en vuelo. Mantener
        // aquí el valor aplicado evita que esa respuesta use fechas sin confirmar.
        this.value = appliedRange;
        openCustomRange();
        return;
      }
      closeCustomRange();
      $("st-range-edit").hidden = true;
      appliedRange = this.value;
      saveLocal("cfg", "all", "x", "visibleRange", this.value);
      loadAll();
    });
    $("st-range-edit").addEventListener("click", openCustomRange);
    $("st-range-cancel").addEventListener("click", function () {
      closeCustomRange();
      $("st-range").value = appliedRange;
      $("st-range-edit").hidden = appliedRange !== "custom";
      $("st-range-from").value = dateValue(loadLocal("cfg", "all", "x", "rangeFrom", ""));
      $("st-range-to").value = dateValue(loadLocal("cfg", "all", "x", "rangeTo", ""));
    });
    $("st-range-apply").addEventListener("click", function () {
      var from = $("st-range-from").value, to = $("st-range-to").value;
      if (!dateValue(from) || !dateValue(to) || from > to) {
        $("st-range-error").textContent = "Elige dos fechas en orden.";
        $("st-range-error").hidden = false;
        return;
      }
      saveLocal("cfg", "all", "x", "rangeFrom", from);
      saveLocal("cfg", "all", "x", "rangeTo", to);
      saveLocal("cfg", "all", "x", "visibleRange", "custom");
      appliedRange = "custom";
      $("st-range").value = "custom";
      $("st-range-edit").textContent = "Cambiar fechas";
      closeCustomRange();
      loadAll();
    });
    $("st-range-dates").addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        $("st-range-cancel").click();
        $("st-range").focus();
      }
    });
    function showArchiveRange(from, to, windowName) {
      if (!chart || !candles.length) return;
      chartBox.dataset.archiveWindow = windowName;
      chart.timeScale().setVisibleRange({ from: from, to: to });
    }
    function showArchivePeriod(period) {
      if (!chart || !candles.length) return;
      var last = candles[candles.length - 1].time;
      var first = candles[0].time;
      if (period === "all") {
        showArchiveRange(first, last, "all");
      } else if (period === "custom") {
        var fromDay = $("st-range-from").value, toDay = $("st-range-to").value;
        if (!fromDay || !toDay || fromDay > toDay) {
          $("st-archive-meta").textContent = "Elige dos fechas en orden.";
          return;
        }
        var requested = requestedWindow(period, last);
        var fromIndex = candles.findIndex(function (c) { return inRequestedWindow(c, requested); });
        var toIndex = -1;
        for (var i = candles.length - 1; i >= 0; i--) {
          if (inRequestedWindow(candles[i], requested)) { toIndex = i; break; }
        }
        if (fromIndex < 0 || toIndex < fromIndex) {
          $("st-archive-meta").textContent = "No hay velas cargadas entre esas fechas.";
          return;
        }
        if (fromIndex === toIndex) {
          chart.timeScale().setVisibleLogicalRange({from:fromIndex - 0.5, to:toIndex + 0.5});
          chartBox.dataset.archiveWindow = "custom";
        } else {
          showArchiveRange(candles[fromIndex].time, candles[toIndex].time, "custom");
        }
        $("st-archive-meta").textContent = "Cobertura cargada: " +
          new Date(first * 1000).toISOString().slice(0, 10) + " → " +
          new Date(last * 1000).toISOString().slice(0, 10) +
          (first > requested.from || studyCandleEnd(last) < requested.to ? ". El rango solicitado está parcialmente cubierto." : ".");
      } else {
        var years = Number(period);
        if (![1, 5, 10].includes(years)) return;
        var fromYear = requestedWindow(period, last).from;
        var fromIndex = candles.findIndex(function (c) { return studyCandleEnd(c.time) > fromYear; });
        showArchiveRange(candles[Math.max(0, fromIndex)].time, last, period);
      }
      $("st-archive-year").value = "";
    }
    function showArchiveYear(year) {
      if (!archiveRequested || !chart || !candles.length) return;
      var first = -1, last = -1;
      for (var i = 0; i < candles.length; i++) {
        if (new Date(candles[i].time * 1000).getUTCFullYear() === Number(year)) {
          if (first < 0) first = i;
          last = i;
        }
      }
      if (first < 0) return;
      var fromIndex = first === last ? Math.max(0, first - 1) : first;
      var toIndex = first === last ? Math.min(candles.length - 1, last + 1) : last;
      showArchiveRange(candles[fromIndex].time, candles[toIndex].time, "year:" + year);
      $("st-archive-year").value = String(year);
    }
    $("st-archive-year").addEventListener("change", function (event) {
      if (event.target.value) showArchiveYear(event.target.value);
    });
    $("st-pair").addEventListener("change", function () {
      /* Mover el foco a una sugerencia no debe cargar la búsqueda parcial. */
      if ($("st-search").hidden) loadAll();
    });
    /* Layouts: selector + Guardar como + Duplicar + Eliminar. */
    loadLayoutsFromLocal();
    paintLayouts();
    U.api("/ux/state?market=__layouts&provider=all&timeframe=x&kind=layouts").then(function (r) {
      if (r.ok && r.data && r.data.payload && r.data.payload.layouts && !Object.keys(layouts).length) {
        layouts = r.data.payload.layouts || {};
        curLayout = r.data.payload.current || null;
        paintLayouts();
      }
    }).catch(function () {});
    if ($("st-layoutsave")) $("st-layoutsave").addEventListener("click", function () {
      var name = ($("st-layoutname").value || "").trim() || ("Estrategia " + (Object.keys(layouts).length + 1));
      layouts[name] = snapshotLayout(name);
      curLayout = name;
      persistLayouts(); paintLayouts("Layout «" + name + "» guardado con " + market + "/" + provider + "/" + timeframe + ".");
      U.showToast("ok", "Layout «" + name + "» guardado.");
    });
    if ($("st-layoutsel")) $("st-layoutsel").addEventListener("change", function (ev) {
      if (ev.target.value) applyLayout(ev.target.value);
    });
    if ($("st-layoutdup")) $("st-layoutdup").addEventListener("click", function () {
      var src = $("st-layoutsel").value || curLayout;
      if (!src || !layouts[src]) { U.showToast("blocked", "Elige un layout para duplicar."); return; }
      var base = src + " copia", n = base, i = 2;
      while (layouts[n]) { n = base + " " + (i++); }
      layouts[n] = JSON.parse(JSON.stringify(layouts[src]));
      layouts[n].name = n;
      curLayout = n;
      persistLayouts(); paintLayouts("Layout duplicado como «" + n + "».");
    });
    if ($("st-layoutdel")) $("st-layoutdel").addEventListener("click", function () {
      var src = $("st-layoutsel").value || curLayout;
      if (!src || !layouts[src]) return;
      delete layouts[src];
      if (curLayout === src) curLayout = null;
      persistLayouts(); paintLayouts("Layout «" + src + "» eliminado.");
    });
    $("st-reset").addEventListener("click", function () {
      unlockPriceAuto();
      try { if (chart && chart.timeScale().fitContent) chart.timeScale().fitContent(); } catch (e) {}
      selectedId = null; setTool(null);
      hideFloatingConfig();
      redrawDrawings();
    });
    function resetTimeScale() {
      try { if (chart && chart.timeScale().fitContent) chart.timeScale().fitContent(); } catch (e) {}
    }
    $("st-zin").addEventListener("click", function () { zoomBy(0.75); });
    $("st-zout").addEventListener("click", function () { zoomBy(1.33); });
    /* Submenú Funciones del Estudio: apertura única (un solo panel visible).
     * Al pulsar una entrada se abre solo ese panel y se cierra el anterior;
     * al pulsar la ya abierta se cierra. Botones nativos: usable con Tab +
     * Enter/Espacio; flechas mueven el foco y Esc cierra el panel abierto. */
    (function armFuncMenu() {
      var btns = Array.prototype.slice.call(root.querySelectorAll("[data-stf]"));
      if (!btns.length) return;
      function panelOf(b) { return root.querySelector("#" + b.getAttribute("aria-controls")); }
      function closeAll() {
        btns.forEach(function (o) {
          o.setAttribute("aria-expanded", "false");
          var p = panelOf(o);
          if (p) p.hidden = true;
        });
      }
      btns.forEach(function (b, i) {
        b.addEventListener("click", function () {
          var was = b.getAttribute("aria-expanded") === "true";
          var p = panelOf(b);
          closeAll();
          if (!was && p) { b.setAttribute("aria-expanded", "true"); p.hidden = false; }
        });
        b.addEventListener("keydown", function (ev) {
          if (ev.key === "ArrowRight" || ev.key === "ArrowDown") {
            ev.preventDefault();
            btns[(i + 1) % btns.length].focus();
          } else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") {
            ev.preventDefault();
            btns[(i - 1 + btns.length) % btns.length].focus();
          } else if (ev.key === "Escape") {
            b.setAttribute("aria-expanded", "false");
            var p = panelOf(b);
            if (p) p.hidden = true;
          }
        });
      });
    })();
    /* Único selector de posición: details compacto dentro de Estudio. */
    (function armDockSel() {
      var sel = $("st-cfg-dock");
      if (sel) {
        sel.value = readDockStored() || dockDefault();
        sel.addEventListener("change", function (ev) { applyDock(ev.target.value); });
      }
    })();
    /* Metadatos del adaptador local evitan elecciones imposibles antes de
     * pedir velas. Un fallo observado sigue prevaleciendo sobre lo declarado. */
    function tfReason(prov, tf) {
      var observed = tfUnavailableReason(prov, tf);
      if (observed) return observed;
      if (prov === "auto" || !declaredTimeframes || !Array.isArray(declaredTimeframes[prov])) return "";
      return declaredTimeframes[prov].indexOf(tf) < 0 ?
        "Intervalo no nativo en " + prov.toUpperCase() : "";
    }
    function updateTfAvailability() {
      var prov = $("st-prov").value;
      var tfSel = $("st-tf");
      var note = $("st-tf-note");
      var blocked = [];
      var observedReasons = [];
      Array.from(tfSel.options).forEach(function (opt) {
        var reason = tfReason(prov, opt.value);
        opt.disabled = !!reason;
        try { opt.title = reason || ""; } catch (e) {}
        if (reason) blocked.push(opt.value + ": " + reason);
        var observed = tfUnavailableReason(prov, opt.value);
        if (observed) observedReasons.push(observed);
      });
      var changed = false;
      var chosen = tfSel.selectedOptions[0];
      if (chosen && chosen.disabled) {
        var fallback = Array.from(tfSel.options).find(function (opt) { return !opt.disabled; });
        if (fallback) {
          tfSel.value = fallback.value;
          timeframe = fallback.value;
          changed = true;
        }
      }
      if (note) {
        note.textContent = observedReasons.length === 1 ? observedReasons[0] :
          changed ? "Intervalo no disponible; se eligió " + timeframe + "." :
          blocked.length ? blocked.length + " intervalos no disponibles con " + prov.toUpperCase() + "." : "";
      }
    }
    function loadDeclaredTimeframes() {
      return U.api("/market/providers", { cache: "no-store" }).then(function (res) {
        if (res.ok && res.data && res.data.timeframes && typeof res.data.timeframes === "object") {
          declaredTimeframes = res.data.timeframes;
        }
      }).catch(function () {
        /* Sin metadatos, el servidor seguirá rechazando intervalos no nativos. */
      }).then(function () {
        if (!root.isConnected || stopping) return;
        updateTfAvailability();
        paintRefreshNote();
        loadAll();
      });
    }
    $("st-prov").addEventListener("change", function () {
      provider = this.value;
      updateTfAvailability();
      loadAll();
    });
    function onDefaultProviderChange(event) {
      var selected = event && event.detail && event.detail.provider;
      if (!selected || !$('st-prov') || $('st-prov').value === selected) return;
      $('st-prov').value = selected;
      provider = selected;
      updateTfAvailability();
      loadAll();
    }
    window.addEventListener("orbis:provider-change", onDefaultProviderChange);
    $("st-tf").addEventListener("change", function () {
      var prov = $("st-prov").value;
      /* M5: solo frena lo ya observado (con su motivo exacto); lo no
       * probado se intenta y, si falla, el available:false honesto lo
       * deshabilita entonces. */
      var seenReason = tfReason(prov, this.value);
      if (seenReason) {
        U.showToast("blocked", this.value + " no disponible para " + prov + ": " + seenReason);
        this.value = "1m";
      }
      if (!$("st-range-dates").hidden) $("st-range-cancel").click();
      timeframe = this.value;
      /* Al cambiar de temporalidad se recalcula el Auto, se muestra el
       * valor efectivo y loadAll reinicia el temporizador solo. */
      paintRefreshNote();
      loadAll();
    });
    $("st-ctype").addEventListener("change", loadAll);
    $("st-save").addEventListener("click", function () {
      persistDrawings(); persistInd();
      U.showToast("ok", "Estudio guardado para " + market + " en " + provider + ".");
    });
    $("st-full").addEventListener("click", function () {
      var sec = root.querySelector(".studio");
      if (!sec) return;
      try {
        if (document.fullscreenElement) { document.exitFullscreen(); return; }
        if (sec.requestFullscreen) { sec.requestFullscreen(); return; }
      } catch (e) {}
      /* Fallback honesto sin Fullscreen API: clase local que replica el
       * layout a pantalla completa. Esc la quita (ver keydown). */
      sec.classList.toggle("studio-expanded");
      fitChartSize();
    });
    /* Recalcula tamaños al entrar/salir de pantalla completa y con
     * ResizeObserver: el gráfico ocupa el alto disponible y al salir el
     * layout vuelve solo.
     * Anti-realimentación (el contenedor #st-chart mide lo que sus canvas
     * ocupan, así que medirlo y redimensionar puede re-disparar al
     * observador): se ignora si el tamaño no cambió (< 1 px) y se ignora
     * la reentrada mientras un ajuste está en curso. Junto con que el
     * vendor interpreta h como alto TOTAL (punto fijo), una vuelta
     * converge en vez de sumar la altura del panel de volumen. */
    var lastFitW = 0, lastFitH = 0, stFitting = false;
    function fitChartSize() {
      if (!chart || !chartBox.isConnected) return;
      if (stFitting) return;
      stFitting = true;
      try {
        var w = chartBox.clientWidth || 600;
        var h = chartBox.clientHeight || 380;
        if (Math.abs(w - lastFitW) < 1 && Math.abs(h - lastFitH) < 1) return;
        lastFitW = w; lastFitH = h;
        if (chart.resize) chart.resize(w, h);
      } catch (e) {} finally { stFitting = false; }
      redrawDrawings();
    }
    function onFullscreenChange() {
      if (!chartBox.isConnected) {
        try { document.removeEventListener("fullscreenchange", onFullscreenChange); } catch (e) {}
        return;
      }
      fitChartSize();
    }
    try { document.addEventListener("fullscreenchange", onFullscreenChange); } catch (e) {}
    var chartRO = null;
    try {
      if (window.ResizeObserver && chartBox) {
        chartRO = new ResizeObserver(function () { fitChartSize(); });
        chartRO.observe(chartBox);
      }
    } catch (e) { chartRO = null; }
    root.querySelectorAll("[data-tool]").forEach(function (b) {
      b.addEventListener("click", function () {
        var t = b.getAttribute("data-tool");
        setTool(activeTool === t ? null : t);
      });
    });
    $("st-undo").addEventListener("click", doUndo);
    $("st-redo").addEventListener("click", doRedo);
    $("st-magnet").addEventListener("change", function (ev) { magnet = ev.target.checked; saveLocal("cfg", "all", "x", "magnet", magnet); });
    $("st-refresh").addEventListener("change", function (ev) {
      var v = ev.target.value;
      if (v === "auto") { refreshMode = "auto"; refreshMs = 0; }
      else {
        refreshMode = Number(v);
        if (REFRESH_MANUAL_VALUES.indexOf(refreshMode) < 0) { refreshMode = "auto"; refreshMs = 0; }
        else refreshMs = refreshMode;
      }
      saveLocal("cfg", "all", "x", "refresh", refreshMode);
      paintRefreshNote();
      restartEstudioTimer();
    });
    /* Nota de refresco en lenguaje llano (Fase 4): distingue los tres
     * conceptos que pide el usuario — duración de vela (timeframe, p. ej.
     * "Vela 1m"), frecuencia de refresco (cada cuánto se consulta, el Auto
     * es la cuarta parte de la vela) y vela abierta (la última, que se
     * actualiza en cada refresco junto al precio). Coherente con lo que el
     * temporizador hace de verdad (refreshEstudioTick: ticker + vela). */
    function paintRefreshNote() {
      var note = $("st-refresh-note"), sel = $("st-refresh");
      if (sel) {
        var autoOpt = sel.querySelector('option[value="auto"]');
        if (autoOpt) autoOpt.textContent = "Automática (" + fmtRefreshMs(autoRefreshFor(timeframe)) + ")";
      }
      if (!note) return;
      var eff = effectiveRefreshMs();
      note.textContent = eff > 0 ? "Vela " + timeframe + " · se actualiza cada " + fmtRefreshMs(eff) + " (precio y vela abierta)." : "Actualización apagada (vela " + timeframe + " congelada).";
      try { note.title = "Duración de vela: " + timeframe + " por vela. Frecuencia de refresco: cada " + (eff > 0 ? fmtRefreshMs(eff) : "—") + " se consulta el precio y la vela abierta (la última vela, que cambia hasta que cierra)."; } catch (e) {}
    }
    function restartEstudioTimer() {
      if (estudioRefreshTimer) { clearInterval(estudioRefreshTimer); estudioRefreshTimer = null; }
      if (archiveRequested) {
        var archiveNote = $("st-refresh-note");
        if (archiveNote) archiveNote.textContent = "Archivo de velas cerradas · sin actualización automática.";
        return;
      }
      if (provider === "grvt" || market.endsWith(":PERP")) {
        var grvtNote = $("st-refresh-note");
        if (grvtNote) grvtNote.textContent = "GRVT PERP: consulta puntual. Streaming automático pendiente; usa Cargar para actualizar.";
        return;
      }
      var effMs = effectiveRefreshMs();
      if (!document.hidden && effMs > 0) {
        estudioRefreshTimer = setInterval(function () {
          if (chartBox.isConnected && !document.hidden) {
            refreshEstudioTick(); /* ticker + vela abierta (antes: solo ticker) */
          }
        }, effMs);
      }
    }
    $("st-cmpadd").addEventListener("click", function () {
      var sym = canon2($("st-cmp").value || "");
      if (!sym || sym.indexOf("/") < 0) return;
      U.api("/market/candles?symbol=" + encodeURIComponent(sym) + "&provider=" + encodeURIComponent(provider) + "&timeframe=" + encodeURIComponent(timeframe) + "&limit=" + candleLimitFor(indCfg)).then(function (r) {
        if (r.ok && r.data && r.data.available) {
          compareSymbol = { symbol: sym, rows: r.data.candles };
          paintCompare();
          U.showToast("ok", sym + " comparada como línea % normalizada.");
        } else {
          U.showToast("blocked", sym + " no disponible en " + provider + " para comparar.");
        }
      }).catch(function () { U.showToast("error", "No hemos podido conectar."); });
    });
    $("st-cmpdel").addEventListener("click", function () {
      compareSymbol = null;
      if (cmpSeries && chart) { try { chart.removeSeries(cmpSeries); } catch (e) {} cmpSeries = null; }
    });
    $("st-aladd").addEventListener("click", function () {
      var pr = $("st-alprice").value.trim();
      addAlert($("st-alop").value === "lt" ? "lt" : "gt", pr);
    });
    /* M4: abren el formulario mínimo con el precio del crosshair (o el
     * último) precargado y editable; el envío usa el helper compartido. */
    $("st-buy-open").addEventListener("click", function () { openTradeForm("buy"); });
    $("st-sell-open").addEventListener("click", function () { openTradeForm("sell"); });
    $("st-trade-cancel").addEventListener("click", closeTradeForm);
    $("st-trade-form").addEventListener("submit", submitTradeForm);
    $("st-mkpaper").addEventListener("change", function (ev) { showPAPER = ev.target.checked; saveLocal("cfg", "all", "x", "showpaper", showPAPER); paintMarkers(); refreshEntryPlan(); /* M3: ligado a la casilla */ });
    $("st-mkdca").addEventListener("change", function (ev) { showDCA = ev.target.checked; showPlans = ev.target.checked; saveLocal("cfg", "all", "x", "showdca", showDCA); paintMarkers(); });
    $("st-wlpush").addEventListener("click", function () {
      var mk = canon2($("st-wladd").value || "");
      if (!mk || mk.indexOf("/") < 0) return;
      var local = loadLocal("__wl", "all", "x", "items", []);
      local.push({ market: mk, provider: provider });
      saveLocal("__wl", "all", "x", "items", local);
      U.api("/ux/watchlist", { method: "POST", body: JSON.stringify({ market: mk, provider: provider }) }).catch(function () {});
      try {
        var wl = JSON.parse(localStorage.getItem("orbis.watchlist") || "[]");
        wl.push(mk); localStorage.setItem("orbis.watchlist", JSON.stringify(wl));
      } catch (e) {}
      paintWatchlist();
    });
    /* keydown a nivel document: se guarda en keyHandler para que cleanup()
     * pueda eliminarlo (un function-expression nombrado inline no deja el
     * nombre visible en este scope y removeEventListener fallaría). */
    keyHandler = function (ev) {
      if (!chartBox.isConnected) { try { document.removeEventListener("keydown", keyHandler); } catch (e) {} stopStream(); return; }
      if (ev.key === "Escape") {
        if (document.fullscreenElement) { try { document.exitFullscreen(); } catch (e) {} }
        var secX = root.querySelector(".studio");
        if (secX && secX.classList.contains("studio-expanded")) {
          secX.classList.remove("studio-expanded");
          fitChartSize();
        }
        /* Si hay un arrastre de edición en curso, se cancela restaurando los
         * puntos originales y retirando su paso de undo (sin fantasma en
         * Ctrl+Z); el arrastre no continúa en el siguiente movimiento porque
         * el estado queda a null. El dragState de creación se limpia en
         * cancelDrawingInteractions, como ya hacía setTool(null). */
        if (editDrag) {
          try { cancelEditDragApply(drawings, editDrag, undoStack, redoStack); } catch (e) {}
        }
        cancelDrawingInteractions();
        setTool(null); selectedId = null; tip.hidden = true; hideFloatingConfig();
        redrawDrawings();
      } else if ((ev.key === "Delete" || ev.key === "Backspace") && selectedId) {
        /* Si hay un arrastre de edición en curso, se cancela primero
         * (restaura puntos + retira su paso de undo diferido): sin esto el
         * editDrag quedaba con id huérfano y el borrado dejaba dos pasos en
         * el undo (el del arrastre + el del borrado). Tras cancelar, el
         * borrado deja exactamente un paso. */
        if (editDrag) {
          try { cancelEditDragApply(drawings, editDrag, undoStack, redoStack); } catch (e) {}
          editDrag = null;
          setNavEnabled(!activeTool);
        }
        var d = drawings.filter(function (o) { return o.id === selectedId; })[0];
        if (d && !d.locked) {
          pushUndo();
          drawings = removeDrawing(drawings, selectedId);
          selectedId = null;
          persistDrawings(); redrawDrawings(); paintObjects();
        }
      } else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") {
        ev.preventDefault();
        if (ev.shiftKey) doRedo(); else doUndo();
      }
    };
    document.addEventListener("keydown", keyHandler);
    /* resize + repinte periódico guardados: cleanup() los elimina (sin esto
     * cada visita a Estudio dejaba un listener y un intervalo de 5 s vivos). */
    winResizeHandler = function () { fitChartSize(); };
    window.addEventListener("resize", winResizeHandler);
    repaintTimer = setInterval(function () { if (chartBox.isConnected && !document.hidden) redrawDrawings(); }, 5000);

    armSearch();
    applyDock(dockDefault(), true);
    loadDockFromServer();
    updateTfAvailability();
    loadDeclaredTimeframes();

    /* Exposed cleanup for router navigation: idempotent, closes WS, timers,
     * listeners and the alert subscription. */
    var cleanup = function () {
      stopping = true;
      /* Invalida peticiones iniciadas en esta vista antes de desmontar el DOM. */
      loadGeneration++;
      /* Sin gestos a medias al salir: aborta creación/edición en curso y
       * restaura la navegación antes de desmontar el chart. */
      try { cancelDrawingInteractions(); } catch (e) {}
      if (estudioRefreshTimer) { try { clearInterval(estudioRefreshTimer); } catch (e) {} estudioRefreshTimer = null; }
      if (ws) { try { ws.close(); } catch (e) {} ws = null; }
      if (pollTimer) { try { clearTimeout(pollTimer); } catch (e) {} try { clearInterval(pollTimer); } catch (e) {} pollTimer = null; }
      stopAlertUpdates();
      try { if (keyHandler) document.removeEventListener("keydown", keyHandler); } catch (e) {}
      keyHandler = null;
      try { if (winResizeHandler) window.removeEventListener("resize", winResizeHandler); } catch (e) {}
      try { window.removeEventListener("orbis:provider-change", onDefaultProviderChange); } catch (e) {}
      winResizeHandler = null;
      if (repaintTimer) { try { clearInterval(repaintTimer); } catch (e) {} repaintTimer = null; }
      try { document.removeEventListener("fullscreenchange", onFullscreenChange); } catch (e) {}
      try { if (chartRO) chartRO.disconnect(); } catch (e) {}
      chartRO = null;
      try { if (window._orbisDockStorageHandler) window.removeEventListener("storage", window._orbisDockStorageHandler); } catch (e) {}
      window._orbisDockStorageHandler = null;
      window._orbisDockStorageArmed = false;
      try { chartBox.removeEventListener("click", onChartClick); } catch (e) {}
      try { chartBox.removeEventListener("mousedown", onChartMouseDown); } catch (e) {}
      try { chartBox.removeEventListener("mousemove", onChartMouseMove); } catch (e) {}
      try { chartBox.removeEventListener("mouseup", onChartMouseUp); } catch (e) {}
      try { chartBox.removeEventListener("mouseleave", onChartMouseLeave); } catch (e) {}
      try { chartBox.removeEventListener("contextmenu", onContextMenu); } catch (e) {}
      try { chartBox.removeEventListener("mousedown", onNavMouseDown); } catch (e) {}
      try { chartBox.removeEventListener("mouseup", onNavMouseUp); } catch (e) {}
      try { chartBox.removeEventListener("mouseleave", onNavMouseLeave); } catch (e) {}
      try { chartBox.removeEventListener("dblclick", onNavDblClick); } catch (e) {}
      if (chart) { try { chart.remove(); } catch (e) {} chart = null; }
      priceSeries = null; volSeries = null; cmpSeries = null; indSeries = {}; indSeriesMeta = {};
      priceMarkers = null; paperMarks = []; planMarks = []; paperOps = []; paperBarGroups = Object.create(null); entryPlan = null; entryLine = null; pnlLine = null; /* M3 */ lastMouse = null; try { hideMarkerTip(); } catch (e) {}
      chartBox.innerHTML = "";
      drawings = []; selectedId = null; activeTool = null; dragState = null; editDrag = null; pendingPts = [];
      undoStack = []; redoStack = [];
      candles = []; hasMore = true; loading = false;
      refreshing = false; refreshLastClose = null;
      lastPrice = null; lastBid = null; lastAsk = null;
      lastTradeQuote = null; lastTradeQuoteReceivedMs = null;
      pendingTradeRequest = null;
      lastCrossPrice = null; tradeSide = null; lastResolvedProvider = null; lastAvailQty = null; /* M4 */
      chartResolvedProvider = null;
    };
    activeCleanup = cleanup;

  }

  window.OrbisEstudio = {
    render: render,
    cleanup: function () {
      if (activeCleanup) {
        var fn = activeCleanup;
        activeCleanup = null;
        fn();
      }
    }
  };
})();
