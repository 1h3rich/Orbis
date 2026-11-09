// SPDX-License-Identifier: MIT
pragma solidity 0.8.38;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
// Only MIT-licensed TickMath, BitMath and CustomRevert are compiled from v4-core.
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";

interface IV3Factory {
    function getPool(address, address, uint24) external view returns (address);
}
interface IV3Pool {
    function factory() external view returns (address);
    function token0() external view returns (address);
    function token1() external view returns (address);
    function fee() external view returns (uint24);
    function liquidity() external view returns (uint128);
    function observe(uint32[] calldata) external view returns (int56[] memory, uint160[] memory);
}
interface IRouter02 {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata) external payable returns (uint256);
}

/// @notice Development kernel. Not deployed, not an operational ORBIS bot yet.
/// @dev Owner wallet retains all outputs. Operator cannot configure or withdraw.
contract BoundedSwapExecutor is ReentrancyGuard {
    using SafeERC20 for IERC20;

    address public constant USDT = 0x55d398326f99059fF775485246999027B3197955;
    address public constant WBNB = 0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c;
    address public constant ROUTER = 0xB971eF87ede563556b2ED4b1C0b0019111Dd85d2;
    address public constant FACTORY = 0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7;
    uint256 public constant UNIT = 1e18;
    address public immutable owner;
    address public immutable pool;
    uint24 public immutable poolFee;

    struct Policy {
        address operator;
        uint64 expiresAt;
        uint64 cooldownSeconds;
        uint32 maxEntries;
        uint32 twapSeconds;
        uint16 maxDeviationBps;
        uint256 maxBuyPerOrder;
        uint256 buyBudget;
        uint256 maxExposure;
        uint256 maxExitNotional;
        uint256 maxRealizedLoss;
        uint256 maxGasPrice;
        uint256 maxWbnbAcquired;
        bool allowBuy;
        bool allowSell;
    }
    Policy private _policy;
    uint256 public policyId;
    uint256 public nextNonce;
    uint256 public buySpent;
    uint256 public entries;
    uint256 public ownedWbnb;
    uint256 public costUsdt;
    uint256 public realizedLoss;
    uint256 public lastEntryAt;
    uint256 public acquiredWbnb;

    error Unauthorized();
    error InvalidPolicy();
    error InvalidMarket();
    error PolicyChanged();
    error NonceMismatch();
    error Expired();
    error BudgetExceeded();
    error ExposureExceeded();
    error LossLimit();
    error Cooldown();
    error OracleUnavailable();
    error InsufficientMinimum();
    error BalanceChanged();
    error PositionExceeded();
    error ExitLimit();
    error TokenBudgetExceeded();
    error PositionChanged();

    event PolicyConfigured(uint256 indexed id, address indexed operator, Policy terms);
    event Stopped(uint256 indexed id, uint256 owned, uint256 cost);
    event TokenRecovered(address indexed token, uint256 amount);
    event NativeRecovered(uint256 amount);
    event PositionDetached(uint256 indexed id, uint256 priorOwned, uint256 priorCost);
    event SwapExecuted(
        uint256 indexed id,
        uint256 indexed nonce,
        bool buy,
        uint256 amountIn,
        uint256 amountOut,
        uint256 owned,
        uint256 cost
    );

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    constructor(address wallet, uint24 fee) {
        if (
            block.chainid != 56 ||
            wallet == address(0) ||
            wallet == address(this) ||
            (fee != 100 && fee != 500 && fee != 3000 && fee != 10000) ||
            ROUTER.code.length == 0 ||
            FACTORY.code.length == 0 ||
            USDT.code.length == 0 ||
            WBNB.code.length == 0 ||
            IERC20Metadata(USDT).decimals() != 18 ||
            IERC20Metadata(WBNB).decimals() != 18
        ) revert InvalidMarket();
        address market = IV3Factory(FACTORY).getPool(USDT, WBNB, fee);
        if (
            market.code.length == 0 ||
            IV3Pool(market).factory() != FACTORY ||
            IV3Pool(market).token0() != USDT ||
            IV3Pool(market).token1() != WBNB ||
            IV3Pool(market).fee() != fee
        ) revert InvalidMarket();
        owner = wallet;
        pool = market;
        poolFee = fee;
    }

    function policy() external view returns (Policy memory) {
        return _policy;
    }

    function configure(uint256 expectedPolicy, Policy calldata terms) external onlyOwner nonReentrant {
        if (expectedPolicy != policyId) revert PolicyChanged();
        bool invalidBuyLimits =
            terms.allowBuy
                ? (terms.cooldownSeconds < 60 ||
                    terms.maxEntries == 0 ||
                    terms.maxBuyPerOrder == 0 ||
                    terms.buyBudget < terms.maxBuyPerOrder ||
                    terms.maxExposure == 0 ||
                    terms.maxRealizedLoss == 0)
                : (terms.cooldownSeconds != 0 ||
                    terms.maxEntries != 0 ||
                    terms.maxBuyPerOrder != 0 ||
                    terms.buyBudget != 0 ||
                    terms.maxExposure != 0 ||
                    terms.maxRealizedLoss != 0);
        if (
            invalidBuyLimits ||
            terms.operator == address(0) ||
            (!terms.allowBuy && !terms.allowSell) ||
            terms.operator == owner ||
            terms.operator == address(this) ||
            terms.operator.code.length != 0 ||
            terms.expiresAt <= block.timestamp ||
            terms.expiresAt > block.timestamp + 1 days ||
            terms.cooldownSeconds > 1 days ||
            terms.maxEntries > 1000 ||
            terms.twapSeconds < 300 ||
            terms.twapSeconds > 1 days ||
            terms.maxDeviationBps == 0 ||
            terms.maxDeviationBps > 500 ||
            terms.maxBuyPerOrder > 1000 * UNIT ||
            terms.buyBudget > 10000 * UNIT ||
            terms.maxExposure > 10000 * UNIT ||
            (terms.allowSell && terms.maxExitNotional == 0) ||
            terms.maxExitNotional > 10000 * UNIT ||
            terms.maxRealizedLoss > 10000 * UNIT ||
            terms.maxGasPrice == 0 ||
            terms.maxGasPrice > 1000 gwei ||
            terms.maxWbnbAcquired == 0 ||
            terms.maxWbnbAcquired > type(uint128).max ||
            terms.maxWbnbAcquired < ownedWbnb
        ) revert InvalidPolicy();
        _policy = terms;
        policyId++;
        nextNonce = 0;
        buySpent = 0;
        entries = 0;
        realizedLoss = 0;
        lastEntryAt = 0;
        acquiredWbnb = ownedWbnb;
        // Position and remaining cost carry across an explicit new grant.
        emit PolicyConfigured(policyId, terms.operator, terms);
    }

    function stop() external nonReentrant {
        if (msg.sender != owner && msg.sender != _policy.operator) revert Unauthorized();
        policyId++;
        _policy.operator = address(0);
        emit Stopped(policyId, ownedWbnb, costUsdt);
    }

    function recoverToken(address token) external onlyOwner nonReentrant {
        uint256 amount = IERC20(token).balanceOf(address(this));
        IERC20(token).safeTransfer(owner, amount);
        emit TokenRecovered(token, amount);
    }

    function recoverNative() external onlyOwner nonReentrant {
        uint256 amount = address(this).balance;
        (bool returned, ) = owner.call{value: amount}("");
        if (!returned) revert BalanceChanged();
        emit NativeRecovered(amount);
    }

    /// @notice Explicit ownership release after an external/manual intervention.
    /// @dev No token transfer, sale price, realized gain or replenished budget is invented.
    function detachPosition(
        uint256 expectedPolicy,
        uint256 expectedOwned,
        uint256 expectedCost
    ) external onlyOwner nonReentrant {
        if (_policy.operator != address(0)) revert InvalidPolicy();
        if (policyId != expectedPolicy) revert PolicyChanged();
        if (expectedOwned != ownedWbnb || expectedCost != costUsdt || ownedWbnb == 0)
            revert PositionChanged();
        ownedWbnb = 0;
        costUsdt = 0;
        policyId++;
        emit PositionDetached(policyId, expectedOwned, expectedCost);
    }

    function executeSwap(
        uint256 expectedPolicy,
        uint256 nonce,
        bool buy,
        uint256 amount,
        uint256 minimum,
        uint256 deadline
    ) external nonReentrant returns (uint256 out) {
        if (msg.sender != _policy.operator) revert Unauthorized();
        if ((buy && !_policy.allowBuy) || (!buy && !_policy.allowSell)) revert Unauthorized();
        if (expectedPolicy != policyId) revert PolicyChanged();
        if (nonce != nextNonce) revert NonceMismatch();
        if (
            block.timestamp >= _policy.expiresAt ||
            block.timestamp > deadline ||
            deadline > _policy.expiresAt ||
            deadline > block.timestamp + 120
        ) revert Expired();
        if (tx.gasprice > _policy.maxGasPrice || amount == 0 || amount > type(uint128).max)
            revert InvalidPolicy();
        if (IERC20(WBNB).balanceOf(owner) < ownedWbnb) revert BalanceChanged();
        if (buy) {
            if (
                amount > _policy.maxBuyPerOrder ||
                buySpent + amount > _policy.buyBudget ||
                entries >= _policy.maxEntries
            ) revert BudgetExceeded();
            if (costUsdt + amount > _policy.maxExposure) revert ExposureExceeded();
            if (realizedLoss >= _policy.maxRealizedLoss) revert LossLimit();
            if (lastEntryAt != 0 && block.timestamp < lastEntryAt + _policy.cooldownSeconds)
                revert Cooldown();
        } else if (amount > ownedWbnb) revert PositionExceeded();
        uint256 fair = _quote(buy, uint128(amount));
        if (buy && ownedWbnb != 0) {
            if (
                ownedWbnb > type(uint128).max ||
                _quote(false, uint128(ownedWbnb)) + amount > _policy.maxExposure
            ) revert ExposureExceeded();
        }
        if (!buy && fair > _policy.maxExitNotional) revert ExitLimit();
        if (fair == 0 || minimum == 0 || minimum < Math.mulDiv(fair, 10000 - _policy.maxDeviationBps, 10000))
            revert InsufficientMinimum();
        uint256 assigned;
        nextNonce++;
        if (buy) {
            buySpent += amount;
            entries++;
            lastEntryAt = block.timestamp;
        } else {
            assigned = amount == ownedWbnb ? costUsdt : Math.mulDiv(costUsdt, amount, ownedWbnb);
            ownedWbnb -= amount;
            costUsdt -= assigned;
        }
        out = _swap(buy, amount, minimum);
        if (buy) {
            ownedWbnb += out;
            costUsdt += amount;
            acquiredWbnb += out;
            if (acquiredWbnb > _policy.maxWbnbAcquired) revert TokenBudgetExceeded();
            if (ownedWbnb > type(uint128).max || _quote(false, uint128(ownedWbnb)) > _policy.maxExposure)
                revert ExposureExceeded();
        } else if (assigned > out) realizedLoss += assigned - out;
        emit SwapExecuted(policyId, nonce, buy, amount, out, ownedWbnb, costUsdt);
    }

    function quoteTwap(bool buy, uint128 amount) external view returns (uint256) {
        return _quote(buy, amount);
    }

    function _quote(bool buy, uint128 amount) private view returns (uint256) {
        if (IV3Pool(pool).liquidity() == 0) revert OracleUnavailable();
        uint32 period = _policy.twapSeconds;
        if (period < 300) revert OracleUnavailable();
        uint32[] memory ago = new uint32[](2);
        ago[0] = period;
        (int56[] memory cumulative, ) = IV3Pool(pool).observe(ago);
        if (cumulative.length != 2) revert OracleUnavailable();
        int56 delta = cumulative[1] - cumulative[0];
        int56 seconds56 = int56(uint56(period));
        int56 average = delta / seconds56;
        if (delta < 0 && delta % seconds56 != 0) average--;
        if (average < -887272 || average > 887272) revert OracleUnavailable();
        uint160 sqrt = TickMath.getSqrtPriceAtTick(int24(average));
        if (sqrt <= type(uint128).max) {
            uint256 ratio = uint256(sqrt) * sqrt;
            return buy ? Math.mulDiv(ratio, amount, 1 << 192) : Math.mulDiv(1 << 192, amount, ratio);
        }
        uint256 ratio128 = Math.mulDiv(sqrt, sqrt, 1 << 64);
        return buy ? Math.mulDiv(ratio128, amount, 1 << 128) : Math.mulDiv(1 << 128, amount, ratio128);
    }

    function _swap(bool buy, uint256 amount, uint256 minimum) private returns (uint256 out) {
        IERC20 input = IERC20(buy ? USDT : WBNB);
        IERC20 output = IERC20(buy ? WBNB : USDT);
        uint256 beforeInput = input.balanceOf(address(this));
        uint256 beforeOwner = input.balanceOf(owner);
        uint256 beforeOutput = output.balanceOf(owner);
        input.safeTransferFrom(owner, address(this), amount);
        if (
            input.balanceOf(address(this)) != beforeInput + amount ||
            input.balanceOf(owner) != beforeOwner - amount
        ) revert BalanceChanged();
        input.forceApprove(ROUTER, amount);
        uint256 reported = IRouter02(ROUTER).exactInputSingle(
            IRouter02.ExactInputSingleParams({
                tokenIn: address(input),
                tokenOut: address(output),
                fee: poolFee,
                recipient: owner,
                amountIn: amount,
                amountOutMinimum: minimum,
                sqrtPriceLimitX96: 0
            })
        );
        input.forceApprove(ROUTER, 0);
        out = output.balanceOf(owner) - beforeOutput;
        if (
            out != reported ||
            out < minimum ||
            input.balanceOf(address(this)) != beforeInput ||
            input.balanceOf(owner) != beforeOwner - amount
        ) revert BalanceChanged();
    }
}
