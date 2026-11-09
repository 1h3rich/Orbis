/* Tasa descriptiva LAB/PAPER; nunca compara rentabilidad ni independencia. */
(function () {
  "use strict";
  function esc(value) { return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  }); }
  function rate(row) {
    if (!row) return "—";
    var value = Number(row.per_100_candles);
    return Number.isFinite(value) ? "≈ " + new Intl.NumberFormat("es-ES", {maximumFractionDigits:1}).format(value) : "—";
  }
  function stamp(value) {
    if (!Number.isSafeInteger(value) || value < 0 || value > 253402300799) return null;
    return new Date(value * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC";
  }
  function scope(label, row) {
    var start = row && stamp(row.first_candle_t);
    var end = row && stamp(row.last_candle_t);
    var help = start && end ? label + " · velas evaluadas del " + start + " al " + end +
      ". Puede haber huecos; no equivale a observaciones independientes." :
      label + " · periodo evaluado no verificable en esta comparación.";
    return '<span class="paper-frequency-scope" tabindex="0" data-help="' + esc(help) + '">' +
      esc(label) + " " + rate(row) + "</span>";
  }
  function burst(label, row, clustered) {
    if (!clustered || !row || !Number.isSafeInteger(row.signal_bursts)) return "";
    return ' <span class="warn" data-help="Muchas señales ' + esc(label) +
      ' aparecen en velas consecutivas. Las rachas son descriptivas; no equivalen a observaciones independientes.">· ' +
      esc(label) + ": " + esc(row.signal_bursts) + (row.signal_bursts === 1 ? " racha" : " rachas") + "</span>";
  }
  function markup(frequency) {
    if (!frequency || (!frequency.historical && !frequency.paper)) return "";
    return '<p class="paper-frequency" data-help="Número de señales por 100 velas realmente evaluadas. LAB y PAPER cubren periodos distintos; no es rendimiento ni prueba de cambio estadístico.">' +
      "Señales / 100 velas · " + scope("LAB", frequency.historical) + " · " + scope("PAPER", frequency.paper) +
      (frequency.historical_small_sample ? ' <span class="warn" data-help="Menos de 30 señales o menos de 30 velas evaluadas en LAB; la tasa es descriptiva e inestable.">· muestra LAB escasa</span>' : "") +
      (frequency.paper_small_sample ? ' <span class="warn" data-help="Menos de 30 señales o menos de 30 velas observadas en PAPER; no infiere rendimiento futuro.">· muestra PAPER escasa</span>' : "") +
      burst("LAB", frequency.historical, frequency.historical_clustered) +
      burst("PAPER", frequency.paper, frequency.paper_clustered) + "</p>";
  }
  window.OrbisSignalFrequency = Object.freeze({markup:markup});
})();
