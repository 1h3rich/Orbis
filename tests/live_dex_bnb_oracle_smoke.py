"""Opt-in public BSC oracle read. No account, keys, signatures or transactions.

    .venv/bin/python -m tests.live_dex_bnb_oracle_smoke --output /tmp/oracle.json

ORBIS_BSC_RPC_URL selects a read node; URLs and provider errors are never logged.
This is not a mainnet order test or an independent consensus proof.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from eth_abi import encode, decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak
import httpx


USDT = "0x55d398326f99059ff775485246999027b3197955"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
FACTORY = "0xdb1d10011ad0ff90774d0c6bb92e5c5c8b4461f7"
ROUTER = "0xb971ef87ede563556b2ed4b1c0b0019111dd85d2"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fee", type=int, choices=(100, 500, 3000, 10000), default=500)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    calls = 0
    with httpx.Client(timeout=8, trust_env=False) as client:
        def rpc(method, params):
            nonlocal calls
            assert method in {"eth_chainId", "eth_getBlockByNumber", "eth_call", "eth_getCode"}
            calls += 1
            response = client.post(os.environ.get("ORBIS_BSC_RPC_URL", "https://bsc-rpc.publicnode.com"),
                json={"jsonrpc": "2.0", "id": calls, "method": method, "params": params})
            response.raise_for_status()
            data = response.json()
            if data.get("id") != calls or "error" in data or "result" not in data:
                raise ValueError("Unverifiable read")
            return data["result"]

        def call(address, signature, inputs=(), values=(), outputs=("uint256",)):
            data = "0x" + (keccak(text=signature)[:4] + encode(inputs, values)).hex()
            return decode(outputs, bytes.fromhex(rpc("eth_call", [{"to": address, "data": data}, block["number"]])[2:]))

        assert int(rpc("eth_chainId", []), 16) == 56
        block = rpc("eth_getBlockByNumber", ["latest", False])
        stamp = datetime.fromtimestamp(int(block["timestamp"], 16), timezone.utc)
        assert -15 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 120
        for address in (FACTORY, ROUTER, USDT, WBNB):
            assert int(rpc("eth_getCode", [address, block["number"]]), 16) > 0
        pool = call(FACTORY, "getPool(address,address,uint24)", ("address", "address", "uint24"),
                    (USDT, WBNB, args.fee), ("address",))[0]
        assert int(pool, 16) > 0
        assert int(rpc("eth_getCode", [pool, block["number"]]), 16) > 0
        assert call(pool, "factory()", outputs=("address",))[0] == FACTORY
        assert call(pool, "token0()", outputs=("address",))[0] == USDT
        assert call(pool, "token1()", outputs=("address",))[0] == WBNB
        assert call(pool, "fee()", outputs=("uint24",))[0] == args.fee
        liquidity = call(pool, "liquidity()", outputs=("uint128",))[0]
        assert liquidity > 0
        for token in (USDT, WBNB):
            assert call(token, "decimals()", outputs=("uint8",))[0] == 18
        cumulative, seconds_per_liquidity = call(pool, "observe(uint32[])", ("uint32[]",),
                                                ([1800, 0],), ("int56[]", "uint160[]"))
        assert len(cumulative) == len(seconds_per_liquidity) == 2
        tick = (cumulative[1] - cumulative[0]) // 1800
        assert -887272 <= tick <= 887272
        assert rpc("eth_getBlockByNumber", [block["number"], False])["hash"] == block["hash"]
        result = {"ok": True, "chain": 56, "block": int(block["number"], 16), "fee": args.fee,
                  "twap_seconds": 1800, "mean_tick": tick, "liquidity": str(liquidity), "calls": calls,
                  "source": "PUBLIC_RPC_OBSERVED", "transactions_sent": 0, "wallet_used": False}
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, ValueError, AssertionError, KeyError, TypeError, DecodingError):
        print(json.dumps({"ok": False, "reason": "Public BSC oracle unavailable or unverified",
                          "transactions_sent": 0, "wallet_used": False}))
        raise SystemExit(1) from None
