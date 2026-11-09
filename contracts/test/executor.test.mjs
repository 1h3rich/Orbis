import { test } from "node:test";
import assert from "node:assert/strict";
import ganache from "ganache";
import { createPublicClient, createWalletClient, custom, parseUnits, toHex, decodeEventLog } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { compile } from "../compiler.mjs";

const USDT = "0x55d398326f99059fF775485246999027B3197955";
const WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c";
const ROUTER = "0xB971eF87ede563556b2ED4b1C0b0019111Dd85d2";
const FACTORY = "0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7";
const POOL = "0x0000000000000000000000000000000000001234";
const keys = ["0x" + "11".repeat(32), "0x" + "22".repeat(32), "0x" + "33".repeat(32)];
const unit = (value) => parseUnits(String(value), 18);
let compilation;
export async function fixture(t, { chainId = 56, usdtDecimals = 18 } = {}) {
    compilation ??= compile({ fixtures: true });
    const provider = ganache.provider({
        chain: { chainId, hardfork: "shanghai", vmErrorsOnRPCResponse: false },
        wallet: { accounts: keys.map((secretKey) => ({ secretKey, balance: "0x3635c9adc5dea00000" })) },
        logging: { quiet: true },
    });
    t.after(() => provider.disconnect());
    const chain = {
        id: chainId,
        name: "Isolated BNB EVM",
        nativeCurrency: { name: "BNB", symbol: "BNB", decimals: 18 },
        rpcUrls: { default: { http: [] } },
    };
    const transport = custom(provider, { retryCount: 0 });
    const client = createPublicClient({ chain, transport, pollingInterval: 10 });
    const wallets = keys.map((key) =>
        createWalletClient({ chain, transport, account: privateKeyToAccount(key) }),
    );
    const [owner, operator, stranger] = wallets;
    const artifact = (name) =>
        compilation.contracts[
            name === "BoundedSwapExecutor" ? "src/BoundedSwapExecutor.sol" : "test/fixtures.sol"
        ][name];
    async function receipt(hash) {
        const result = await client.waitForTransactionReceipt({ hash });
        assert.equal(result.status, "success");
        return result;
    }
    async function write(address, name, functionName, args = [], wallet = owner) {
        return receipt(await wallet.writeContract({ address, abi: artifact(name).abi, functionName, args }));
    }
    async function read(address, name, functionName, args = []) {
        return client.readContract({ address, abi: artifact(name).abi, functionName, args });
    }
    for (const [address, name] of [
        [USDT, usdtDecimals === 6 ? "MockSixDecimals" : "MockToken"],
        [WBNB, "MockToken"],
        [ROUTER, "MockRouter"],
        [FACTORY, "MockFactory"],
        [POOL, "MockPool"],
    ]) {
        await provider.request({
            method: "evm_setAccountCode",
            params: [address, "0x" + artifact(name).evm.deployedBytecode.object],
        });
    }
    await write(POOL, "MockPool", "set", [0, 0n, false]);
    await write(ROUTER, "MockRouter", "set", [10000n, false]);
    for (const token of [USDT, WBNB]) {
        await write(token, "MockToken", "mint", [owner.account.address, unit(1000)]);
        await write(token, "MockToken", "mint", [ROUTER, unit(10000)]);
    }
    const deployed = await receipt(
        await owner.deployContract({
            abi: artifact("BoundedSwapExecutor").abi,
            bytecode: "0x" + artifact("BoundedSwapExecutor").evm.bytecode.object,
            args: [owner.account.address, 500],
        }),
    );
    const address = deployed.contractAddress;
    for (const token of [USDT, WBNB]) await write(token, "MockToken", "approve", [address, unit(1000)]);
    const now = (await client.getBlock()).timestamp;
    const policy = {
        operator: operator.account.address,
        expiresAt: now + 3600n,
        cooldownSeconds: 60n,
        maxEntries: 10,
        twapSeconds: 1800,
        maxDeviationBps: 50,
        maxBuyPerOrder: unit(100),
        buyBudget: unit(200),
        maxExposure: unit(150),
        maxExitNotional: unit(1000),
        maxRealizedLoss: unit(20),
        maxGasPrice: 100000000000n,
        maxWbnbAcquired: unit(500),
        allowBuy: true,
        allowSell: true,
    };
    await write(address, "BoundedSwapExecutor", "configure", [0n, policy]);
    return {
        provider,
        client,
        owner,
        operator,
        stranger,
        address,
        policy,
        write,
        read,
        unit,
        USDT,
        WBNB,
        ROUTER,
        POOL,
        abi: artifact("BoundedSwapExecutor").abi,
        async advance(seconds) {
            await provider.request({ method: "evm_increaseTime", params: [seconds] });
            await provider.request({ method: "evm_mine", params: [] });
        },
        async configure(changes) {
            return write(address, "BoundedSwapExecutor", "configure", [
                await read(address, "BoundedSwapExecutor", "policyId"),
                { ...policy, ...changes },
            ]);
        },
        async execute(
            buy,
            amount,
            minimum = (amount * 9950n) / 10000n,
            nonce = 0n,
            epoch = null,
            wallet = operator,
        ) {
            const at = BigInt(
                (await provider.request({ method: "eth_getBlockByNumber", params: ["latest", false] }))
                    .timestamp,
            );
            return write(
                address,
                "BoundedSwapExecutor",
                "executeSwap",
                [
                    epoch ?? (await read(address, "BoundedSwapExecutor", "policyId")),
                    nonce,
                    buy,
                    amount,
                    minimum,
                    at + 120n,
                ],
                wallet,
            );
        },
    };
}

test("operator buys inside the grant; output stays in owner wallet and router approval returns to zero", async (t) => {
    const f = await fixture(t);
    const before = await f.read(WBNB, "MockToken", "balanceOf", [f.owner.account.address]);
    await f.execute(true, unit(25));
    assert.equal(await f.read(USDT, "MockToken", "balanceOf", [f.owner.account.address]), unit(975));
    assert.equal(await f.read(WBNB, "MockToken", "balanceOf", [f.owner.account.address]), before + unit(25));
    assert.equal(await f.read(USDT, "MockToken", "balanceOf", [f.address]), 0n);
    assert.equal(await f.read(USDT, "MockToken", "allowance", [f.address, ROUTER]), 0n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "ownedWbnb"), unit(25));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "costUsdt"), unit(25));
});

test("partial and complete sales use actual output, conserve acquisition cost and leave pre-existing WBNB untouched", async (t) => {
    const f = await fixture(t);
    await f.write(ROUTER, "MockRouter", "set", [9975n, false]);
    await f.execute(true, unit(25));
    const position = unit("24.9375");
    await assert.rejects(() => f.execute(false, position + 1n, 1n, 1n));
    await f.execute(false, unit("8.3125"), unit("8.2709375"), 1n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "costUsdt"), 16666666666666666667n);
    await f.execute(false, unit("16.625"), unit("16.541875"), 2n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "ownedWbnb"), 0n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "costUsdt"), 0n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "realizedLoss"), unit("0.12484375"));
    assert.equal(await f.read(WBNB, "MockToken", "balanceOf", [f.owner.account.address]), unit(1000));
    assert.equal(
        await f.read(USDT, "MockToken", "balanceOf", [f.owner.account.address]),
        unit("999.87515625"),
    );
    assert.equal(await f.read(WBNB, "MockToken", "allowance", [f.address, ROUTER]), 0n);
});

test("owner STOP invalidates queued orders; a new grant preserves the position and rejects the previous policy ID", async (t) => {
    const f = await fixture(t);
    await f.execute(true, unit(10));
    await assert.rejects(() =>
        f.write(f.address, "BoundedSwapExecutor", "configure", [1n, f.policy], f.operator),
    );
    await assert.rejects(() => f.write(f.address, "BoundedSwapExecutor", "stop", [], f.stranger));
    await f.write(f.address, "BoundedSwapExecutor", "stop");
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "policyId"), 2n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "ownedWbnb"), unit(10));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "costUsdt"), unit(10));
    await assert.rejects(() => f.execute(false, unit(10), unit("9.95"), 1n));
    await f.write(f.address, "BoundedSwapExecutor", "configure", [2n, f.policy]);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "acquiredWbnb"), unit(10));
    await assert.rejects(() => f.execute(false, unit(10), unit("9.95"), 0n, 1n));
    await f.execute(false, unit(10), unit("9.95"), 0n, 3n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "ownedWbnb"), 0n);
});

test("the permission is expired at its exact deadline, including a call simulated in that block", async (t) => {
    const f = await fixture(t);
    await f.provider.request({ method: "evm_setTime", params: [Number(f.policy.expiresAt * 1000n)] });
    await f.provider.request({ method: "evm_mine", params: [] });
    const block = await f.client.getBlock();
    assert.equal(block.timestamp, f.policy.expiresAt);
    const abi = compilation.contracts["src/BoundedSwapExecutor.sol"].BoundedSwapExecutor.abi;
    await assert.rejects(() =>
        f.client.simulateContract({
            address: f.address,
            abi,
            functionName: "executeSwap",
            args: [1n, 0n, true, unit(25), unit("24.875"), f.policy.expiresAt],
            account: f.operator.account,
            blockNumber: block.number,
        }),
    );
});

test("only the granted operator can trade and neither a trade replay nor an extra wei over the order cap is accepted", async (t) => {
    const f = await fixture(t);
    await assert.rejects(() => f.execute(true, unit(25), unit("24.875"), 0n, null, f.stranger));
    await assert.rejects(() => f.execute(true, unit(100) + 1n));
    await f.execute(true, unit(25));
    await assert.rejects(() => f.execute(true, unit(25), unit("24.875"), 0n));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "buySpent"), unit(25));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "nextNonce"), 1n);
});

test("selling does not replenish the cumulative purchase budget", async (t) => {
    const f = await fixture(t);
    await f.configure({ maxBuyPerOrder: unit(25), buyBudget: unit(25) });
    await f.execute(true, unit(25));
    await f.execute(false, unit(25), unit("24.875"), 1n);
    await f.advance(61);
    await assert.rejects(() => f.execute(true, unit(1), unit(".995"), 2n));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "buySpent"), unit(25));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "ownedWbnb"), 0n);
});

test("exposure limits prevent another purchase while still allowing a full exit", async (t) => {
    const f = await fixture(t);
    await f.configure({ maxExposure: unit(25) });
    await f.execute(true, unit(25));
    await f.advance(61);
    await assert.rejects(() => f.execute(true, unit(1), unit(".995"), 1n));
    await f.execute(false, unit(25), unit("24.875"), 1n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "costUsdt"), 0n);
});

test("entry count and cooldown do not block reductions", async (t) => {
    const f = await fixture(t);
    await f.configure({ maxEntries: 1 });
    await f.execute(true, unit(25));
    await assert.rejects(() => f.execute(true, unit(1), unit(".995"), 1n));
    await f.execute(false, unit(25), unit("24.875"), 1n);
    await f.advance(61);
    await assert.rejects(() => f.execute(true, unit(1), unit(".995"), 2n));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "entries"), 1n);
});

test("cooldown ends after its period without resetting spent budget or nonce", async (t) => {
    const f = await fixture(t);
    await f.execute(true, unit(25));
    await assert.rejects(() => f.execute(true, unit(1), unit(".995"), 1n));
    await f.advance(61);
    await f.execute(true, unit(1), unit(".995"), 1n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "buySpent"), unit(26));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "nextNonce"), 2n);
});

test("realized losses halt further purchases but do not prevent reducing the rest", async (t) => {
    const f = await fixture(t);
    await f.configure({ maxRealizedLoss: unit(".01") });
    await f.write(ROUTER, "MockRouter", "set", [9975n, false]);
    await f.execute(true, unit(25));
    await f.execute(false, unit("8.3125"), unit("8.2709375"), 1n);
    await f.advance(61);
    await assert.rejects(() => f.execute(true, unit(1), unit(".995"), 2n));
    await f.execute(false, unit("16.625"), unit("16.541875"), 2n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "realizedLoss"), unit("0.12484375"));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "ownedWbnb"), 0n);
});

test("the operator can stop its own authority without gaining owner configuration rights", async (t) => {
    const f = await fixture(t);
    await f.execute(true, unit(25));
    await f.write(f.address, "BoundedSwapExecutor", "stop", [], f.operator);
    await assert.rejects(() => f.execute(false, unit(25), unit("24.875"), 1n));
    await assert.rejects(() =>
        f.write(f.address, "BoundedSwapExecutor", "configure", [1n, f.policy], f.operator),
    );
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "ownedWbnb"), unit(25));
});

test("a caller cannot weaken the independent TWAP minimum or trade without oracle history", async (t) => {
    const f = await fixture(t);
    await assert.rejects(() => f.execute(true, unit(25), 1n));
    await f.write(POOL, "MockPool", "set", [0, 0n, true]);
    await assert.rejects(() => f.execute(true, unit(25)));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "nextNonce"), 0n);
    assert.equal(await f.read(USDT, "MockToken", "balanceOf", [f.owner.account.address]), unit(1000));
});

test("negative fractional average ticks round down rather than truncating toward zero", async (t) => {
    const f = await fixture(t);
    await f.write(POOL, "MockPool", "set", [0, -1n, false]);
    assert.equal(
        await f.read(f.address, "BoundedSwapExecutor", "quoteTwap", [true, unit(1)]),
        (unit(1) * 10000n) / 10001n,
    );
});

test("a mined swap failure rolls back tokens, approval, nonce and all purchase accounting", async (t) => {
    const f = await fixture(t);
    await f.write(ROUTER, "MockRouter", "set", [10000n, true]);
    const at = (await f.client.getBlock()).timestamp;
    const hash = await f.operator.writeContract({
        address: f.address,
        abi: f.abi,
        functionName: "executeSwap",
        args: [1n, 0n, true, unit(25), unit("24.875"), at + 120n],
        gas: 1500000n,
    });
    const receipt = await f.client.waitForTransactionReceipt({ hash });
    assert.equal(receipt.status, "reverted");
    assert(receipt.gasUsed > 0n);
    for (const name of ["nextNonce", "buySpent", "entries", "ownedWbnb", "costUsdt"])
        assert.equal(await f.read(f.address, "BoundedSwapExecutor", name), 0n);
    assert.equal(await f.read(USDT, "MockToken", "balanceOf", [f.owner.account.address]), unit(1000));
    assert.equal(await f.read(USDT, "MockToken", "allowance", [f.address, ROUTER]), 0n);
    assert.equal(await f.read(USDT, "MockToken", "balanceOf", [f.address]), 0n);
});

test("exposure also uses the current TWAP value when an existing position appreciates", async (t) => {
    const f = await fixture(t);
    await f.configure({ maxExposure: unit(40) });
    await f.execute(true, unit(25));
    await f.advance(61);
    await f.write(POOL, "MockPool", "set", [-6932, 0n, false]);
    await f.write(ROUTER, "MockRouter", "set", [5000n, false]);
    const minimum =
        ((await f.read(f.address, "BoundedSwapExecutor", "quoteTwap", [true, unit(1)])) * 9950n) / 10000n;
    await assert.rejects(() => f.execute(true, unit(1), minimum, 1n));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "costUsdt"), unit(25));
});

test("the actual purchased quantity must remain inside the TWAP exposure limit after execution", async (t) => {
    const f = await fixture(t);
    await f.configure({ maxExposure: unit(40) });
    await f.write(ROUTER, "MockRouter", "set", [20000n, false]);
    await assert.rejects(() => f.execute(true, unit(25)));
    assert.equal(await f.read(USDT, "MockToken", "balanceOf", [f.owner.account.address]), unit(1000));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "ownedWbnb"), 0n);
});

test("only the owner recovers stray tokens; recovery neither sells the position nor gives the operator custody", async (t) => {
    const f = await fixture(t);
    await f.execute(true, unit(25));
    await f.write(WBNB, "MockToken", "mint", [f.address, unit(3)]);
    await assert.rejects(() => f.write(f.address, "BoundedSwapExecutor", "recoverToken", [WBNB], f.operator));
    await f.write(f.address, "BoundedSwapExecutor", "recoverToken", [WBNB]);
    assert.equal(await f.read(WBNB, "MockToken", "balanceOf", [f.address]), 0n);
    assert.equal(await f.read(WBNB, "MockToken", "balanceOf", [f.owner.account.address]), unit(1028));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "ownedWbnb"), unit(25));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "costUsdt"), unit(25));
});

test("forced native BNB can only return to the owner, with exact gas accounting", async (t) => {
    const f = await fixture(t);
    const amount = unit(".01");
    await f.provider.request({ method: "evm_setAccountBalance", params: [f.address, toHex(amount)] });
    await assert.rejects(() => f.write(f.address, "BoundedSwapExecutor", "recoverNative", [], f.operator));
    const before = await f.client.getBalance({ address: f.owner.account.address });
    const receipt = await f.write(f.address, "BoundedSwapExecutor", "recoverNative");
    assert.equal(await f.client.getBalance({ address: f.address }), 0n);
    assert.equal(
        await f.client.getBalance({ address: f.owner.account.address }),
        before + amount - receipt.gasUsed * receipt.effectiveGasPrice,
    );
});

test("a finite WBNB acquisition budget makes one initial approval sufficient and is not replenished by sales", async (t) => {
    const f = await fixture(t);
    await f.configure({ maxWbnbAcquired: unit(25) });
    await f.write(WBNB, "MockToken", "approve", [f.address, unit(25)]);
    await f.execute(true, unit(25));
    await f.execute(false, unit(25), unit("24.875"), 1n);
    assert.equal(await f.read(WBNB, "MockToken", "allowance", [f.owner.account.address, f.address]), 0n);
    await f.advance(61);
    await assert.rejects(() => f.execute(true, unit(1), unit(".995"), 2n));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "acquiredWbnb"), unit(25));
});

test("external disposal requires STOP and an explicit owner detachment, without inventing a sale or realized profit", async (t) => {
    const f = await fixture(t);
    await f.execute(true, unit(25));
    await f.write(WBNB, "MockToken", "transfer", [f.stranger.account.address, unit(1025)]);
    await assert.rejects(() => f.execute(false, unit(25), unit("24.875"), 1n));
    await assert.rejects(() =>
        f.write(f.address, "BoundedSwapExecutor", "detachPosition", [1n, unit(25), unit(25)]),
    );
    await f.write(f.address, "BoundedSwapExecutor", "stop");
    await assert.rejects(() =>
        f.write(f.address, "BoundedSwapExecutor", "detachPosition", [2n, unit(25), unit(25)], f.operator),
    );
    await assert.rejects(() =>
        f.write(f.address, "BoundedSwapExecutor", "detachPosition", [1n, unit(25), unit(25)]),
    );
    const receipt = await f.write(f.address, "BoundedSwapExecutor", "detachPosition", [
        2n,
        unit(25),
        unit(25),
    ]);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "ownedWbnb"), 0n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "costUsdt"), 0n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "buySpent"), unit(25));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "realizedLoss"), 0n);
    assert.equal(receipt.logs.length, 1);
    assert.equal(
        decodeEventLog({ abi: f.abi, data: receipt.logs[0].data, topics: receipt.logs[0].topics }).eventName,
        "PositionDetached",
    );
});

test("an exit cap permits bounded partial reductions without replenishing entry budgets", async (t) => {
    const f = await fixture(t);
    await f.configure({ maxExitNotional: unit(10) });
    await f.execute(true, unit(25));
    await assert.rejects(() => f.execute(false, unit(25), unit("24.875"), 1n));
    await f.execute(false, unit(10), unit("9.95"), 1n);
    await f.execute(false, unit(10), unit("9.95"), 2n);
    await f.execute(false, unit(5), unit("4.975"), 3n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "ownedWbnb"), 0n);
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "buySpent"), unit(25));
    assert.equal(await f.read(USDT, "MockToken", "balanceOf", [f.owner.account.address]), unit(1000));
});

test("the owner cannot accidentally grant the main wallet operator rights, unlimited token amounts or an excessive lifetime", async (t) => {
    const f = await fixture(t);
    const now = (await f.client.getBlock()).timestamp;
    for (const changes of [
        { operator: f.owner.account.address },
        { maxBuyPerOrder: unit(1000) + 1n, buyBudget: unit(2000), maxExposure: unit(2000) },
        { buyBudget: unit(10000) + 1n },
        { maxWbnbAcquired: 2n ** 128n },
        { expiresAt: now + 90000n },
        { maxDeviationBps: 501 },
        { twapSeconds: 299 },
    ])
        await assert.rejects(() => f.configure(changes));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "policyId"), 1n);
});

test("a purchase-only DCA grant cannot sell the acquired position", async (t) => {
    const f = await fixture(t);
    await f.configure({ allowSell: false });
    await f.execute(true, unit(25));
    await assert.rejects(() => f.execute(false, unit(25), unit("24.875"), 1n));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "ownedWbnb"), unit(25));
});

test("a close-only grant has no entry budget and can reduce a previously stopped position", async (t) => {
    const f = await fixture(t);
    await f.execute(true, unit(25));
    await f.write(f.address, "BoundedSwapExecutor", "stop");
    await f.configure({
        allowBuy: false,
        allowSell: true,
        maxBuyPerOrder: 0n,
        buyBudget: 0n,
        maxEntries: 0,
        cooldownSeconds: 0n,
        maxExposure: 0n,
        maxRealizedLoss: 0n,
        maxWbnbAcquired: unit(25),
    });
    const policy = await f.read(f.address, "BoundedSwapExecutor", "policy");
    assert.equal(policy.buyBudget, 0n);
    assert.equal(policy.allowBuy, false);
    await assert.rejects(() => f.execute(true, unit(1)));
    await f.execute(false, unit(25), unit("24.875"));
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "ownedWbnb"), 0n);
    assert.equal(await f.read(USDT, "MockToken", "balanceOf", [f.owner.account.address]), unit(1000));
});

test("configuration consumes the reviewed policy revision and an operator STOP wins over a queued renewal", async (t) => {
    const f = await fixture(t);
    await f.write(f.address, "BoundedSwapExecutor", "configure", [1n, f.policy]);
    await f.write(f.address, "BoundedSwapExecutor", "stop", [], f.operator);
    await assert.rejects(() => f.write(f.address, "BoundedSwapExecutor", "configure", [2n, f.policy]));
    const policy = await f.read(f.address, "BoundedSwapExecutor", "policy");
    assert.equal(policy.operator, "0x0000000000000000000000000000000000000000");
    assert.equal(await f.read(f.address, "BoundedSwapExecutor", "policyId"), 3n);
});

test("deployment rejects another chain and Ethereum-style six-decimal USDT", async (t) => {
    await assert.rejects(() => fixture(t, { chainId: 1 }));
    await assert.rejects(() => fixture(t, { usdtDecimals: 6 }));
});
