/* MEXC: only own final reconciled and financially booked fills. */
(function () {
  "use strict";
  window.OrbisMexcBotChart = window.OrbisRealFillChart.create({
    path:function (account,item) { return "/accounts/"+encodeURIComponent(account)+"/mexc-bots/"+encodeURIComponent(item.id)+"/executions-page"; },
    researchExport:{
      path:function (account,item) { return "/accounts/"+encodeURIComponent(account)+"/mexc-bots/"+encodeURIComponent(item.id)+"/research-export"; },
      schema:"orbis.mexc.real.bot-fills.v1",confirmation:"EXPORTAR_JSONL_MEXC_REAL",filename:"orbis-mexc-real",
      prompt:"¿Descargar los fills MEXC REAL en un JSONL sin cifrar?",
      help:"Descarga hasta 5.000 fills asentados en un archivo local sin cifrar.",
      singular:" fill exportado",plural:" fills exportados"
    },
    matches:function (page,item,account) { return page.bot_id === item.id && page.account_id === account && page.symbol === item.symbol && page.timeframe === item.timeframe &&
      page.environment === "REAL" && page.provider === "mexc" && page.evidence === "OWN_FINAL_SETTLED_BOOKED_FILLS"; },
    fillMatches:function (fill) { return typeof fill.event_id === "string"; },
    description:function (fill) { return "Pedido "+fill.intent_id.slice(0,8)+" · Asiento "+fill.event_id.slice(0,8); },
    caption:function () { return "MEXC · REAL"; },
    storageKey:"orbis.chartRange.mexcRealBot",ariaLabel:"Velas MEXC y fills REAL del bot",
    help:"Sólo fills propios finales, conciliados y asentados."
  });
})();
