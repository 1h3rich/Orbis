"""Lectura optativa Uniswap v3 mainnet, sin wallet, claves ni transacciones.

Ejecutar manualmente: .venv/bin/python tests/live_uniswap_quote_smoke.py
``ORBIS_ETH_RPC_URL`` puede señalar un RPC de lectura propio.
"""

from __future__ import annotations

import os

import httpx
from eth_utils import keccak


RPC = os.environ.get("ORBIS_ETH_RPC_URL", "https://ethereum-rpc.publicnode.com")
QUOTER = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
USDC = int("a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 16)
WETH = int("c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", 16)


def rpc(client: httpx.Client, method: str, params: list) -> object:
    response = client.post(RPC, json={"jsonrpc": "2.0", "id": 1,
                                      "method": method, "params": params})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or "error" in payload or "result" not in payload:
        raise RuntimeError("RPC Ethereum no devolvió una lectura verificable")
    return payload["result"]


def main() -> None:
    selector = keccak(text="quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]
    if selector.hex() != "c6a5026a":
        raise RuntimeError("Selector ABI Uniswap inesperado")
    quotes = []
    with httpx.Client(timeout=10, trust_env=False) as client:
        chain = rpc(client, "eth_chainId", [])
        if chain != "0x1":
            raise RuntimeError("RPC no está en Ethereum mainnet")
        block = rpc(client, "eth_blockNumber", [])
        if not isinstance(block, str) or not block.startswith("0x") or int(block, 16) <= 0:
            raise RuntimeError("Bloque RPC inválido")
        for fee in (100, 500, 3000, 10000):
            calldata = "0x" + selector.hex() + "".join(
                f"{value:064x}" for value in (USDC, WETH, 1_000_000, fee, 0)
            )
            try:
                result = rpc(client, "eth_call", [{"to": QUOTER, "data": calldata}, block])
            except RuntimeError:
                continue  # Pool ausente, error RPC o respuesta no verificable: no es cero.
            if not isinstance(result, str) or not result.startswith("0x") or len(result) != 258:
                continue
            amount_out = int(result[2:66], 16)
            gas_estimate = int(result[194:258], 16)
            if amount_out > 0 and gas_estimate > 0:
                quotes.append((amount_out, fee))
    if not quotes:
        raise RuntimeError("Ningún pool directo devolvió cotización verificable")
    best_amount, best_fee = max(quotes)
    assert best_amount > 0
    print(f"Uniswap v3 USDC/WETH: {len(quotes)}/4 lecturas en bloque "
          f"{int(block, 16)}; mayor salida bruta en pool {best_fee / 10000:g} %; "
          "sin transacciones ni wallet")


if __name__ == "__main__":
    main()
