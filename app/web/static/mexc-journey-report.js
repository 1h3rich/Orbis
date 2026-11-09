/* Shared readout for archived MEXC wallet and direct-credit journeys. */
(function () {
  "use strict";

  const deposits = {PENDING:"Por acreditar", CREDITED:"Por contabilizar", BOOKED:"Contabilizado"};
  const withdrawals = {MISSING:"Por solicitar", PENDING:"Por comprobar", RECEIVED:"Por contabilizar",
    BOOKED:"Contabilizada", OTHER_WALLET:"Otra wallet", OTHER_ROUTE:"Otra ruta",
    DESTINATION_UNVERIFIED:"Destino por verificar · conecta la wallet original"};
  const origins = {MANUAL:"Manual", BOT:"Bot", ORBIS:"ORBIS"};
  const destinations = {SPOT:"Abrir operativa Spot", WITHDRAWAL:"Abrir retirada"};

  function quantity(value) {
    return value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
  }

  function order(item) {
    return item ? item.symbol + " · " + origins[item.origin] : "Pendiente";
  }

  function netOrder(item) {
    if (!item) return null;
    const amounts = [item.base_asset, item.quote_asset].map(asset => {
      const value = quantity(item.net_by_asset[asset]);
      return (value.startsWith("-") ? "−" + value.slice(1) : "+" + value) + " " + asset;
    });
    const fees = Object.entries(item.fee_by_asset).filter(([, value]) => quantity(value) !== "0")
      .map(([asset, value]) => quantity(value) + " " + asset);
    amounts.push("Comisión " + (fees.length ? fees.join(" + ") : "0"));
    return amounts.join(" · ");
  }

  function render(container, result, hidden, openStep) {
    const direct = result.source_wallet === "UNKNOWN";
    const credit = result.deposit_amounts;
    const receipt = result.withdrawal_amounts;
    const creditText = credit && quantity(credit.credited) + " USDT" +
      (direct ? "" : " · gas " + quantity(credit.gas_bnb) + " BNB");
    const receiptText = receipt && "Neto " + quantity(receipt.received) + " USDT · comisión " +
      quantity(receipt.actual_fee) + " USDT · débito " + quantity(receipt.actual_debit) + " USDT";
    const title = document.createElement("strong");
    title.textContent = result.complete ? (direct ? "Tramo comprobado" : "Recorrido comprobado") : "Recorrido incompleto";
    title.dataset.help = "Secuencia de movimientos contabilizados en la cuenta MEXC. Los importes corresponden a cada paso.";
    const steps = document.createElement("ul");
    const rows = [
      ["Ingreso · " + deposits[result.deposit], creditText],
      ["Compra Spot · " + order(result.purchase), netOrder(result.purchase)],
      ["Venta Spot · " + order(result.sale), netOrder(result.sale)],
      [(direct ? "Retirada BNB · " : "Retirada · ") + withdrawals[result.withdrawal], receiptText],
    ];
    rows.forEach(([label, amount]) => {
      const step = document.createElement("li");
      step.textContent = label;
      if (amount) {
        const detail = document.createElement("span");
        detail.dataset.fundingPrivate = "";
        detail.orbisVisible = " · " + amount;
        detail.orbisMasked = " · Importes ocultos";
        detail.textContent = hidden ? detail.orbisMasked : detail.orbisVisible;
        step.append(detail);
      }
      steps.append(step);
    });
    container.replaceChildren(title, steps);
    if (direct) {
      const origin = document.createElement("small");
      origin.className = "muted";
      origin.textContent = result.credits + (result.credits === 1 ? " abono" : " abonos") + " · Wallet de origen sin identificar";
      title.after(document.createElement("br"), origin);
    }
    const action = destinations[result.next_step];
    if (action && openStep) {
      const button = document.createElement("button");
      button.className = "ghost";
      button.type = "button";
      button.textContent = action;
      button.dataset.help = "Abre el apartado de la cuenta seleccionada.";
      button.onclick = () => openStep(result.next_step);
      container.append(button);
    }
  }

  window.OrbisMexcJourneyReport = {render};
})();
