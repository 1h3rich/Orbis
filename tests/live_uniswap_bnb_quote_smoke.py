"""Opcional, sólo lectura pública BSC; no acepta wallet, firma ni envía fondos.

    .venv/bin/python -m tests.live_uniswap_bnb_quote_smoke

ORBIS_BSC_RPC_URL permite elegir nodo. No se imprime su URL ni sus errores.
No es una prueba de ejecución mainnet: sólo contratos, ABI y cotización.
"""

import os
from datetime import datetime, timezone

from eth_abi import decode, encode
import httpx

from app.portfolio.wallet_swap import TOKENS, QUOTER, ROUTER


def main():
    count = 0
    with httpx.Client(timeout=10) as client:
        def rpc(method, params):
            nonlocal count
            count += 1
            reply = client.post(os.environ.get("ORBIS_BSC_RPC_URL", "https://bsc-rpc.publicnode.com"),
                json={"jsonrpc": "2.0", "id": count, "method": method, "params": params})
            reply.raise_for_status()
            body = reply.json()
            if body.get("id") != count or "error" in body or "result" not in body:
                raise RuntimeError("Respuesta RPC no válida")
            return body["result"]

        assert int(rpc("eth_chainId", []), 16) == 56
        block = rpc("eth_getBlockByNumber", ["latest", False])
        at = datetime.fromtimestamp(int(block["timestamp"], 16), timezone.utc)
        assert -15 <= (datetime.now(timezone.utc)-at).total_seconds() <= 120
        for contract in (QUOTER, ROUTER, *TOKENS.values()):
            assert int(rpc("eth_getCode", [contract, block["number"]]), 16) > 0
        for contract in TOKENS.values():
            assert int(rpc("eth_call", [{"to": contract, "data": "0x313ce567"}, block["number"]]), 16) == 18
        routes = []
        for fee in (100, 500, 3000, 10000):
            try:
                data = "0xc6a5026a"+encode(["address", "address", "uint256", "uint24", "uint160"],
                    [TOKENS["USDT"], TOKENS["WBNB"], 10**18, fee, 0]).hex()
                raw = rpc("eth_call", [{"to": QUOTER, "data": data}, block["number"]])
                output, sqrt, _ticks, gas = decode(["uint256", "uint160", "uint32", "uint256"], bytes.fromhex(raw[2:]))
                assert output > 0 and sqrt > 0 and gas > 0
                routes.append((fee, str(output)))
            except (httpx.HTTPError, ValueError, RuntimeError):
                continue
        assert routes, "Ningún pool directo respondió"
        print({"chain": 56, "block": int(block["number"], 16), "quotes_usdt_to_wbnb_units": routes,
               "calls": count, "transactions_sent": 0, "wallet_used": False})


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, ValueError, RuntimeError, AssertionError, KeyError, TypeError):
        print("BSC no verificable en esta consulta pública; cero transacciones y ninguna wallet usada.")
        raise SystemExit(1) from None
