// Test-only JSON-RPC bridge to an in-memory EVM. Never connects to mainnet.
import fs from "node:fs";
import path from "node:path";
import http from "node:http";
import { randomUUID } from "node:crypto";
import ganache from "ganache";
import { createPublicClient, createWalletClient, custom } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { compile } from "../compiler.mjs";
import { anvilProvider } from "./anvil-provider.mjs";

const directory = process.env.ORBIS_DEX_FIXTURE_DIRECTORY;
if (!directory) throw new Error("Isolated fixture directory required");
const fixtureId = randomUUID();
const keys = ["0x" + "11".repeat(32), "0x" + "22".repeat(32)];
const provider =
    process.env.ORBIS_DEX_FIXTURE_BACKEND === "anvil"
        ? await anvilProvider(keys)
        : ganache.provider({
              chain: { chainId: 56, hardfork: "shanghai", vmErrorsOnRPCResponse: false },
              wallet: { accounts: keys.map((secretKey) => ({ secretKey, balance: "0x3635c9adc5dea00000" })) },
              logging: { quiet: true },
          });
const chain = {
    id: 56,
    name: "Isolated EVM",
    nativeCurrency: { name: "BNB", symbol: "BNB", decimals: 18 },
    rpcUrls: { default: { http: [] } },
};
const transport = custom(provider, { retryCount: 0 });
const owner = privateKeyToAccount(keys[0]);
const wallet = createWalletClient({ chain, transport, account: owner });
const client = createPublicClient({ chain, transport, pollingInterval: 10 });
const compilation = compile({ fixtures: true }).contracts["test/fixtures.sol"];
const addresses = {
    usdt: "0x55d398326f99059fF775485246999027B3197955",
    wbnb: "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    router: "0xB971eF87ede563556b2ED4b1C0b0019111Dd85d2",
    factory: "0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7",
    pool: "0x0000000000000000000000000000000000001234",
};
for (const [address, name] of [
    [addresses.usdt, "MockToken"],
    [addresses.wbnb, "MockToken"],
    [addresses.router, "MockRouter"],
    [addresses.factory, "MockFactory"],
    [addresses.pool, "MockPool"],
]) {
    await provider.request({
        method: "evm_setAccountCode",
        params: [address, "0x" + compilation[name].evm.deployedBytecode.object],
    });
}
await client.waitForTransactionReceipt({
    hash: await wallet.writeContract({
        address: addresses.pool,
        abi: compilation.MockPool.abi,
        functionName: "set",
        args: [0, 0n, false],
    }),
});
const server = http.createServer(async (request, response) => {
    let body = "";
    let id = null;
    try {
        for await (const part of request) {
            body += part;
            if (body.length > 500000) throw new Error("Oversized fixture request");
        }
        const input = JSON.parse(body);
        id = input.id;
        const result =
            input.method === "orbis_fixture_identity"
                ? { id: fixtureId, chain: 56 }
                : await provider.request({ method: input.method, params: input.params ?? [] });
        response.setHeader("content-type", "application/json");
        response.end(JSON.stringify({ jsonrpc: "2.0", id: input.id, result }));
    } catch (error) {
        response.setHeader("content-type", "application/json");
        response.end(
            JSON.stringify({
                jsonrpc: "2.0",
                id,
                error: {
                    code: Number.isInteger(error.code) ? error.code : -32000,
                    message: error.message,
                    data: error.data,
                },
            }),
        );
    }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
fs.writeFileSync(
    path.join(directory, "ready.json"),
    JSON.stringify({
        fixtureId,
        rpc: `http://127.0.0.1:${server.address().port}`,
        owner: owner.address,
        operator: privateKeyToAccount(keys[1]).address,
        ...addresses,
    }),
);
const stop = () =>
    server.close(async () => {
        await provider.disconnect();
        process.exit(0);
    });
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
