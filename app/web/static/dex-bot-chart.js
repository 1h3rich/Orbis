/* DEX receipts are executions, not financial bookings or MEXC fills. */
(function () {
  "use strict";
  window.OrbisDexBotChart = window.OrbisRealFillChart.create({
    path:function (account,item) { return "/accounts/"+encodeURIComponent(account)+"/dex-bots/runs/"+encodeURIComponent(item.id)+"/executions-page"; },
    researchExport:{
      path:function (account,item) { return "/accounts/"+encodeURIComponent(account)+"/dex-bots/runs/"+encodeURIComponent(item.id)+"/research-export"; },
      schema:"orbis.uniswap.real.bot-swaps.v1",confirmation:"EXPORTAR_JSONL_UNISWAP_REAL",filename:"orbis-uniswap-real",
      prompt:"¿Descargar los swaps confirmados de Uniswap REAL en un JSONL sin cifrar?",
      help:"Descarga hasta 5.000 swaps confirmados en un archivo local sin cifrar.",
      singular:" swap exportado",plural:" swaps exportados"
    },
    item:function (run) { return {id:run.id,symbol:run.source.symbol,timeframe:run.source.timeframe}; },
    signature:function (run) { return JSON.stringify([run.intents,run.review_required,run.stored_status]); },
    matches:function (page,item,account) { return page.run_id === item.id && page.account_id === account && page.symbol === item.symbol && page.timeframe === item.timeframe &&
      page.environment === "REAL" && page.provider === "uniswap-v3" && page.chain_id === "0x38" && page.reference_provider === "mexc" &&
      page.base_asset === "WBNB" && page.quote_asset === "USDT" && typeof page.financially_booked === "boolean" && page.authorization === false &&
      page.evidence === "OWN_FINALIZED_DEX_RECEIPTS" && Number.isInteger(page.pending_count) && page.pending_count >= 0 &&
      Number.isInteger(page.reverted_count) && page.reverted_count >= 0; },
    fillMatches:function (fill) { return fill.fill_id === fill.intent_id && fill.fee_asset === "BNB" && typeof fill.block_number === "string" && /^[1-9][0-9]*$/.test(fill.block_number) && typeof fill.run_id === "string" && typeof fill.partial === "boolean" &&
      typeof fill.financially_booked === "boolean" && (fill.financially_booked ? typeof fill.event_id === "string" && typeof fill.booking_id === "string" : fill.event_id === null && fill.booking_id === null) &&
      typeof fill.reference_price === "string" && /^[0-9]+(?:\.[0-9]+)?$/.test(fill.reference_price); },
    description:function (fill) { return "Recibo "+fill.intent_id.slice(0,8)+" · Bloque "+fill.block_number+(fill.activation_kind ? " · "+fill.activation_kind : ""); },
    detail:function (fill,data,exact) { return "Referencia MEXC · "+exact(fill.reference_price)+" USDT · Señal "+new Date(fill.signal_candle_t*1000).toISOString()+(fill.financially_booked ? " · Asiento "+fill.event_id.slice(0,8) : " · Pendiente de contabilizar"); },
    caption:function (data) { return "Uniswap · REAL · Referencia MEXC"+(data.pending_count ? " · "+data.pending_count+" pendientes" : "")+
      (data.reverted_count ? " · "+data.reverted_count+" revertidas" : ""); },
    storageKey:"orbis.chartRange.dexRealBot",ariaLabel:"Velas de referencia MEXC y ejecuciones confirmadas Uniswap",
    help:"Sólo recibos propios confirmados. Precio e instante de ejecución Uniswap; velas y referencia MEXC. El gas se expresa en BNB."
  });
})();
