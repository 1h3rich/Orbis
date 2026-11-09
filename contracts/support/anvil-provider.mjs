// Test-only protocol engine for real same-nonce replacement races. No forks.
import { spawn } from "node:child_process";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createWalletClient, http } from "viem";
import { privateKeyToAccount } from "viem/accounts";

export async function anvilProvider(keys) {
    const reservation = net.createServer();
    await new Promise((resolve) => reservation.listen(0, "127.0.0.1", resolve));
    const port = reservation.address().port;
    await new Promise((resolve) => reservation.close(resolve));
    const executable = path.resolve(
        path.dirname(fileURLToPath(import.meta.url)),
        "../node_modules/.bin/anvil",
    );
    const child = spawn(
        executable,
        [
            "--host",
            "127.0.0.1",
            "--port",
            String(port),
            "--accounts",
            "0",
            "--chain-id",
            "56",
            "--hardfork",
            "shanghai",
            "--quiet",
            "--timestamp",
            String(Math.floor(Date.now() / 1000) - 1),
        ],
        { stdio: ["ignore", "ignore", "inherit"] },
    );
    let stopped = false;
    const onExit = () => {
        if (!stopped) child.kill("SIGTERM");
    };
    process.once("exit", onExit);
    const url = `http://127.0.0.1:${port}`;
    let sequence = 0;
    async function rpc(method, params = []) {
        if (child.exitCode !== null) throw new Error("Identified Anvil child exited");
        const response = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ jsonrpc: "2.0", id: ++sequence, method, params }),
            signal: AbortSignal.timeout(5000),
        });
        const value = await response.json();
        if (value.error) throw Object.assign(new Error(value.error.message), value.error);
        return value.result;
    }
    const deadline = Date.now() + 15000;
    while (true) {
        try {
            if ((await rpc("eth_chainId")) === "0x38") break;
        } catch {
            /* same identified child only */
        }
        if (child.exitCode !== null || Date.now() > deadline) {
            onExit();
            throw new Error("Owned Anvil fixture did not become ready");
        }
        await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const chain = {
        id: 56,
        name: "Owned replacement fixture",
        nativeCurrency: { name: "BNB", symbol: "BNB", decimals: 18 },
        rpcUrls: { default: { http: [url] } },
    };
    const accounts = keys.map(privateKeyToAccount);
    for (const account of accounts) await rpc("anvil_setBalance", [account.address, "0x3635c9adc5dea00000"]);
    return {
        async request({ method, params = [] }) {
            if (method === "eth_sendTransaction") {
                const tx = params[0];
                const account = accounts.find((item) => item.address.toLowerCase() === tx.from.toLowerCase());
                if (!account || (tx.chainId !== undefined && BigInt(tx.chainId) !== 56n))
                    throw new Error("Unknown fixture signer or chain");
                const value = {
                    to: tx.to ?? undefined,
                    data: tx.data ?? tx.input ?? "0x",
                    value: BigInt(tx.value ?? 0),
                };
                for (const key of ["gas", "gasPrice", "maxFeePerGas", "maxPriorityFeePerGas"])
                    if (tx[key] !== undefined) value[key] = BigInt(tx[key]);
                if (tx.nonce !== undefined) value.nonce = Number(BigInt(tx.nonce));
                if (tx.type !== undefined)
                    value.type =
                        BigInt(tx.type) === 0n
                            ? "legacy"
                            : BigInt(tx.type) === 2n
                              ? "eip1559"
                              : "unsupported";
                else if (tx.gasPrice !== undefined) value.type = "legacy";
                if (tx.accessList !== undefined) value.accessList = tx.accessList;
                return createWalletClient({
                    account,
                    chain,
                    transport: http(url, { retryCount: 0 }),
                }).sendTransaction(value);
            }
            if (method === "evm_setAccountCode") method = "anvil_setCode";
            if (method === "miner_stop") {
                await rpc("evm_setAutomine", [false]);
                return true;
            }
            if (method === "miner_start") {
                await rpc("evm_setAutomine", [true]);
                return true;
            }
            if (method === "evm_setTime") {
                await rpc("evm_setNextBlockTimestamp", [Math.floor(params[0] / 1000)]);
                return 0;
            }
            // This fixture deliberately finalizes immediately, as our Ganache
            // fixture does; it tests execution/mempool rules, not consensus.
            if (
                [
                    "eth_getBlockByNumber",
                    "eth_getCode",
                    "eth_getBalance",
                    "eth_getTransactionCount",
                    "eth_call",
                ].includes(method)
            ) {
                params = params.map((item) => (item === "finalized" ? "latest" : item));
            }
            return rpc(method, params);
        },
        async disconnect() {
            stopped = true;
            process.removeListener("exit", onExit);
            if (child.exitCode === null) {
                child.kill("SIGTERM");
                await Promise.race([
                    new Promise((resolve) => child.once("exit", resolve)),
                    new Promise((resolve) => setTimeout(resolve, 3000)),
                ]);
                if (child.exitCode === null) child.kill("SIGKILL");
            }
        },
    };
}
