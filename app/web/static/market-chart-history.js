/* Carga acotada de velas anteriores de una sola fuente y temporalidad. */
(function () {
  "use strict";
  var MAX_PAGES = 12;

  async function collect(initial, requestPage, options) {
    var rows = initial.candles;
    var pages = 1;
    var exhausted = !!initial.history_exhausted;
    var gaps = !!initial.gap_count;
    var problem = null;
    var hasMore = !!initial.older_pagination_supported && !exhausted;
    var current = options.current || function () { return true; };

    while (hasMore && !options.reached(Number(rows[0].t)) && current()) {
      if (pages >= MAX_PAGES) {
        problem = "limit";
        break;
      }
      var before = Number(rows[0].t);
      if (!Number.isSafeInteger(before) || before <= 0) {
        problem = "cursor";
        hasMore = false;
        break;
      }
      var reply;
      try { reply = await requestPage(before); }
      catch (error) { reply = null; }
      if (!current()) break;
      var page = reply && reply.ok && reply.data;
      if (!page || !page.available || page.resolved_provider !== initial.resolved_provider ||
          page.timeframe !== options.timeframe || (page.symbol && page.symbol !== options.symbol) ||
          !Array.isArray(page.candles)) {
        problem = "source";
        hasMore = false;
        break;
      }
      var previous = -Infinity;
      var coherent = page.candles.every(function (bar) {
        var time = Number(bar && bar.t);
        if (!Number.isSafeInteger(time) || time <= 0 || time <= previous || time >= before) return false;
        previous = time;
        return true;
      });
      if (!coherent || (!page.candles.length && !page.history_exhausted)) {
        problem = "cursor";
        hasMore = false;
        break;
      }
      rows = page.candles.concat(rows);
      pages += 1;
      gaps = gaps || !!page.gap_count || !!page.boundary_gap_seconds;
      exhausted = !!page.history_exhausted;
      hasMore = !exhausted && page.older_pagination_supported !== false;
    }
    return {rows:rows, hasMore:hasMore, exhausted:exhausted, gaps:gaps, problem:problem};
  }

  function yearsBefore(time, years) {
    var end = new Date(time * 1000);
    var year = end.getUTCFullYear() - years;
    var month = end.getUTCMonth();
    var lastDay = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
    return Date.UTC(year, month, Math.min(end.getUTCDate(), lastDay)) / 1000;
  }

  window.OrbisMarketHistory = {collect:collect, yearsBefore:yearsBefore};
})();
