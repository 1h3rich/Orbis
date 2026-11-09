import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import solc from "solc";

const root = path.dirname(fileURLToPath(import.meta.url));
export function compile({ fixtures = false } = {}) {
    if (!solc.version().startsWith("0.8.38+")) throw new Error("Pinned Solidity compiler required");
    const sources = {};
    const dependencies = {};
    for (const filename of ["src/BoundedSwapExecutor.sol", ...(fixtures ? ["test/fixtures.sol"] : [])]) {
        sources[filename] = { content: fs.readFileSync(path.join(root, filename), "utf8") };
    }
    const settings = {
        optimizer: { enabled: true, runs: 200 },
        evmVersion: "paris",
        metadata: { bytecodeHash: "none" },
        outputSelection: {
            "*": {
                "": ["ast"],
                "*": [
                    "abi",
                    "evm.bytecode.object",
                    "evm.deployedBytecode.object",
                    "evm.deployedBytecode.immutableReferences",
                ],
            },
        },
    };
    const output = JSON.parse(
        solc.compile(JSON.stringify({ language: "Solidity", sources, settings }), {
            import(filename) {
                if (
                    !filename.startsWith("@openzeppelin/contracts/") &&
                    !filename.startsWith("@uniswap/v4-core/")
                )
                    return { error: "Import not allowed: " + filename };
                const resolved = path.resolve(root, "node_modules", filename);
                if (!resolved.startsWith(path.join(root, "node_modules") + path.sep))
                    return { error: "Import outside dependencies" };
                const contents = fs.readFileSync(resolved, "utf8");
                if (
                    filename.startsWith("@uniswap/v4-core/") &&
                    !contents.startsWith("// SPDX-License-Identifier: MIT\n")
                )
                    return { error: "Only MIT math is permitted from v4-core" };
                dependencies[filename] = createHash("sha256").update(contents).digest("hex");
                return { contents };
            },
        }),
    );
    const errors = (output.errors ?? []).filter((error) => error.severity === "error");
    if (errors.length) throw new Error(errors.map((error) => error.formattedMessage).join("\n"));
    return {
        contracts: output.contracts,
        ast: output.sources["src/BoundedSwapExecutor.sol"].ast,
        sources,
        settings,
        dependencies: Object.fromEntries(Object.entries(dependencies).sort()),
        compiler: solc.version(),
        warnings: output.errors ?? [],
    };
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
    const result = compile();
    const contract = result.contracts["src/BoundedSwapExecutor.sol"].BoundedSwapExecutor;
    const definition = result.ast.nodes.find(
        (node) => node.nodeType === "ContractDefinition" && node.name === "BoundedSwapExecutor",
    );
    const immutableSlots = Object.fromEntries(
        definition.nodes
            .filter((node) => node.nodeType === "VariableDeclaration" && node.mutability === "immutable")
            .map((node) => [node.name, contract.evm.deployedBytecode.immutableReferences[String(node.id)]]),
    );
    if (Object.keys(immutableSlots).sort().join(",") !== "owner,pool,poolFee")
        throw new Error("Unexpected immutable bindings");
    const artifact = {
        name: "BoundedSwapExecutor",
        compiler: result.compiler,
        settings: result.settings,
        abi: contract.abi,
        bytecode: "0x" + contract.evm.bytecode.object,
        runtimeTemplate: "0x" + contract.evm.deployedBytecode.object,
        immutableReferences: contract.evm.deployedBytecode.immutableReferences,
        immutableSlots,
        sourceSha256: createHash("sha256")
            .update(result.sources["src/BoundedSwapExecutor.sol"].content)
            .digest("hex"),
        dependencies: result.dependencies,
        mainnetDeployed: false,
    };
    fs.mkdirSync(path.join(root, "artifacts"), { recursive: true });
    fs.writeFileSync(
        path.join(root, "artifacts/BoundedSwapExecutor.json"),
        JSON.stringify(artifact, null, 2) + "\n",
    );
    console.log(
        JSON.stringify({
            compiler: result.compiler,
            runtimeBytes: contract.evm.deployedBytecode.object.length / 2,
            sourceSha256: artifact.sourceSha256,
            mainnetDeployed: false,
        }),
    );
}
