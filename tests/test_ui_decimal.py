"""La estimación PAPER del navegador no pierde los dígitos del Ledger."""

import json
import subprocess
from decimal import Decimal, ROUND_DOWN, localcontext
from pathlib import Path


MODULE = Path(__file__).resolve().parent.parent / "app/web/static/decimal.js"


def _estimate(kind, *values):
    script = "const d=require(process.argv[1]); console.log(JSON.stringify(d[process.argv[2]](...process.argv.slice(3))))"
    result = subprocess.run(
        ["node", "-e", script, str(MODULE), kind, *values],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _wide(kind, *values):
    script = "const d=require(process.argv[1]); console.log(JSON.stringify(d[process.argv[2]](...JSON.parse(process.argv[3]))))"
    result = subprocess.run(
        ["node", "-e", script, str(MODULE), kind, json.dumps(values)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_large_buy_estimate_is_exact_to_ledger_unit():
    amount = "123456789012345678.123456789012"
    fee = "0.000000000001"
    result = _estimate("estimateBuy", amount, fee, "101")
    with localcontext() as context:
        context.prec = 80
        expected = ((Decimal(amount) - Decimal(fee)) / Decimal("101")).quantize(
            Decimal("0.000000000001"), rounding=ROUND_DOWN,
        )
    assert result == {
        "total": amount,
        "fee": fee,
        "quantity": format(expected, "f"),
    }


def test_sell_estimate_preserves_cents_and_floors_ledger_unit():
    quantity, fee, price = "1234567890123.123456789012", "0.000000000001", "99.123456789012"
    result = _estimate("estimateSell", quantity, fee, price)
    with localcontext() as context:
        context.prec = 80
        gross = Decimal(quantity) * Decimal(price)
        net = (gross - Decimal(fee)).quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)
    assert result == {"gross": format(gross, "f"), "net": format(net, "f"), "fee": fee}


def test_invalid_input_does_not_silently_become_zero_fee():
    assert _estimate("estimateBuy", "100", "x", "101") is None
    assert _estimate("estimateBuy", "100", "100", "101") is None
    assert _estimate("estimateSell", "0.1", "100", "99") is None
    assert _estimate("estimateBuy", "1.0000000000001", "0", "101") is None
    assert _estimate("estimateSell", "0.000000000001", "0", "0.1") is None


def test_wide_decimal_sums_and_orders_positions_without_binary_rounding():
    values = ["123456789012345678.120000000001", "0.000000000009", "-0.02"]
    assert _wide("sumStrings", values) == "123456789012345678.100000000010"
    assert _wide("compareStrings", "123456789012345678.120000000001",
                 "123456789012345678.120000000002") == -1
    assert _wide("compareStrings", "-0.01", "0") == -1
    assert _wide("compareStrings", "1.20", "1.2") == 0
    assert _wide("sumStrings", ["1", "bad"]) is None
    assert _wide("compareStrings", "NaN", "1") is None
