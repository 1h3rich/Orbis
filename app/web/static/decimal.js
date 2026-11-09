/* Aritmética decimal para estimados y agregados visibles PAPER: sin Number.
 * Sólo presentación; el motor y los límites definitivos están en el servidor.
 * Las órdenes usan el límite del Ledger (18 enteros, 12 decimales); sumas y
 * comparaciones aceptan agregados mayores sin cambiar ese límite. */
(function (root) {
  "use strict";

  function power(scale) { return 10n ** BigInt(scale); }

  function parse(value) {
    if (typeof value !== "string" || value.length > 80) return null;
    var match = /^\s*([+-]?)(\d+)(?:\.(\d+))?\s*$/.exec(value);
    if (!match) return null;
    var whole = match[2].replace(/^0+(?=\d)/, "");
    var fraction = match[3] || "";
    if (whole.length > 18 || fraction.length > 12) return null;
    var units = BigInt(whole + fraction);
    return { units: match[1] === "-" ? -units : units, scale: fraction.length };
  }

  /* Importes derivados pueden superar los 18 dígitos de una orden individual.
   * Se aceptan sólo cadenas decimales acotadas; nunca se convierten a Number. */
  function parseWide(value) {
    if (typeof value !== "string" || value.length > 192) return null;
    var match = /^\s*([+-]?)(\d+)(?:\.(\d+))?\s*$/.exec(value);
    if (!match) return null;
    var whole = match[2].replace(/^0+(?=\d)/, "");
    var fraction = match[3] || "";
    if (whole.length > 80 || fraction.length > 80) return null;
    var units = BigInt(whole + fraction);
    return { units: match[1] === "-" ? -units : units, scale: fraction.length };
  }

  function text(value) {
    var negative = value.units < 0n;
    var digits = String(negative ? -value.units : value.units).padStart(value.scale + 1, "0");
    var whole = value.scale ? digits.slice(0, -value.scale) : digits;
    var fraction = value.scale ? "." + digits.slice(-value.scale) : "";
    return (negative ? "-" : "") + whole + fraction;
  }

  function subtract(left, right) {
    var scale = Math.max(left.scale, right.scale);
    return { units: left.units * power(scale - left.scale) -
      right.units * power(scale - right.scale), scale: scale };
  }

  function multiply(left, right) {
    return { units: left.units * right.units, scale: left.scale + right.scale };
  }

  function compareStrings(leftText, rightText) {
    var left = parseWide(leftText), right = parseWide(rightText);
    if (!left || !right) return null;
    var scale = Math.max(left.scale, right.scale);
    var a = left.units * power(scale - left.scale);
    var b = right.units * power(scale - right.scale);
    return a < b ? -1 : a > b ? 1 : 0;
  }

  function sumStrings(values) {
    if (!Array.isArray(values)) return null;
    var total = { units: 0n, scale: 0 };
    for (var i = 0; i < values.length; i++) {
      var value = parseWide(values[i]);
      if (!value) return null;
      var scale = Math.max(total.scale, value.scale);
      total = { units: total.units * power(scale - total.scale) +
        value.units * power(scale - value.scale), scale: scale };
    }
    return text(total);
  }

  function floorTo(value, places) {
    return { units: value.scale <= places
      ? value.units * power(places - value.scale)
      : value.units / power(value.scale - places), scale: places };
  }

  function divideFloor(left, right, places) {
    if (right.units <= 0n || left.units < 0n) return null;
    return { units: left.units * power(right.scale + places) /
      (right.units * power(left.scale)), scale: places };
  }

  function estimateBuy(amountText, feeText, priceText) {
    var amount = parse(amountText), fee = parse(feeText), price = parse(priceText);
    if (!amount || !fee || !price || amount.units <= 0n || fee.units < 0n || price.units <= 0n) return null;
    var net = subtract(amount, fee);
    if (net.units <= 0n) return null;
    var quantity = divideFloor(net, price, 12);
    if (!quantity || quantity.units <= 0n) return null;
    return { total: text(amount), fee: text(fee), quantity: text(quantity) };
  }

  function estimateSell(quantityText, feeText, priceText) {
    var quantity = parse(quantityText), fee = parse(feeText), price = parse(priceText);
    if (!quantity || !fee || !price || quantity.units <= 0n || fee.units < 0n || price.units <= 0n) return null;
    var gross = multiply(quantity, price);
    var net = subtract(gross, fee);
    if (net.units <= 0n) return null;
    var ledgerNet = floorTo(net, 12);
    if (ledgerNet.units <= 0n) return null;
    return { gross: text(gross), net: text(ledgerNet), fee: text(fee) };
  }

  var api = { estimateBuy: estimateBuy, estimateSell: estimateSell,
    compareStrings: compareStrings, sumStrings: sumStrings };
  root.OrbisDecimal = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
