"""Sólo API local y bóveda temporal. Ningún cliente de firma o envío del servidor."""

import os
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI

from app.portfolio.api import create_real_router
from app.web.mount import register_ui


app = FastAPI()
mined = {}
blocks = {}


@app.post("/__test/mined")
def publish_mined(evidence: dict):
    mined[evidence["tx"]["hash"]] = evidence
    blocks[evidence["block"]["number"]] = evidence


@contextmanager
def reader():
    def read(method, params):
        if method == "eth_chainId":
            return "0x38"
        if method in {"eth_getTransactionByHash", "eth_getTransactionReceipt"}:
            item = mined.get(params[0])
            return item["tx" if method == "eth_getTransactionByHash" else "receipt"] if item else None
        if method == "eth_getBlockByNumber":
            tag = next(reversed(blocks)) if params[0] == "finalized" else params[0]
            return blocks[tag]["block"]
        item = blocks[params[1]]
        if method == "eth_getCode":
            return "0x" if item["tx"]["from"].lower() == params[0].lower() else "0x60006000"
        if method == "eth_getTransactionCount":
            return item["nonce"]
        if method == "eth_call":
            selector = params[0]["data"][:10]
            return {"0x1698ee82": item["pool"], "0xdd62ed3e": item["allowance"]}[selector]
        raise AssertionError(f"Unexpected read: {method}")

    yield read


app.include_router(create_real_router(Path(os.environ["ORBIS_TEST_SWAP_DIRECTORY"]) / "real.db", dex_rpc_factory=reader))
register_ui(app)
