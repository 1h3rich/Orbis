// SPDX-License-Identifier: MIT
pragma solidity 0.8.38;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract MockToken is ERC20 {
    constructor() ERC20("EVM fixture only", "TEST") {}
    function mint(address to, uint256 value) external {
        _mint(to, value);
    }
}

contract MockSixDecimals is MockToken {
    function decimals() public pure override returns (uint8) {
        return 6;
    }
}

contract MockPool {
    int24 public tick;
    int56 public remainder;
    bool public unavailable;
    uint128 public liquidity = 1;
    function set(int24 value, int56 extra, bool missing) external {
        tick = value;
        remainder = extra;
        unavailable = missing;
        liquidity = 1;
    }
    function factory() external pure returns (address) {
        return 0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7;
    }
    function token0() external pure returns (address) {
        return 0x55d398326f99059fF775485246999027B3197955;
    }
    function token1() external pure returns (address) {
        return 0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c;
    }
    function fee() external pure returns (uint24) {
        return 500;
    }
    // Fixture only: model the pool paying the manual swap's output token.
    function pay(address token, address recipient, uint256 amount) external {
        ERC20(token).transfer(recipient, amount);
    }
    function observe(
        uint32[] calldata intervals
    ) external view returns (int56[] memory values, uint160[] memory secondsPerLiquidity) {
        require(!unavailable, "No oracle history");
        values = new int56[](2);
        secondsPerLiquidity = new uint160[](2);
        values[0] = 0;
        values[1] = int56(tick) * int56(uint56(intervals[0])) + remainder;
    }
}

contract MockFactory {
    function getPool(address, address, uint24) external pure returns (address) {
        return address(0x1234);
    }
}

contract MockRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    uint256 public rateBps;
    bool public revertSwap;
    function set(uint256 rate, bool fail) external {
        rateBps = rate;
        revertSwap = fail;
    }
    // Exercise the actual Router02 wallet ABI and atomic delegatecall path.
    function multicall(
        uint256 deadline,
        bytes[] calldata calls
    ) external payable returns (bytes[] memory results) {
        require(block.timestamp <= deadline && msg.value == 0, "Fixture deadline/value");
        results = new bytes[](calls.length);
        for (uint256 cursor; cursor < calls.length; ++cursor) {
            (bool ok, bytes memory returned) = address(this).delegatecall(calls[cursor]);
            require(ok, "Fixture inner call reverted");
            results[cursor] = returned;
        }
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 out) {
        require(!revertSwap, "Swap failure");
        require(params.fee == 500 && params.sqrtPriceLimitX96 == 0, "Wrong pool");
        out = (params.amountIn * rateBps) / 10000;
        require(out >= params.amountOutMinimum, "Insufficient output");
        if (msg.sender == params.recipient) {
            address pool = address(0x1234);
            ERC20(params.tokenIn).transferFrom(msg.sender, pool, params.amountIn);
            MockPool(pool).pay(params.tokenOut, params.recipient, out);
        } else {
            ERC20(params.tokenIn).transferFrom(msg.sender, address(this), params.amountIn);
            ERC20(params.tokenOut).transfer(params.recipient, out);
        }
    }
}
