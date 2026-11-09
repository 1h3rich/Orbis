"""Riesgo e historial del OCO, revalidables al reclamar, enviar y leer copias."""

from dataclasses import asdict
from decimal import Decimal, localcontext
import re

from app.derivatives.paper_math import MarginRules
from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError, _parent, _number
from app.live_execution.grvt_testnet_order import GrvtTestnetLimits
from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote, guard_testnet_limit_price
from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskState, evaluate_testnet_risk
from app.live_execution.grvt_testnet_signer import GrvtTestnetLimitDraft
from app.live_execution.grvt_testnet_continuity import validate_testnet_history_continuity
from app.live_execution.grvt_testnet_reconciliation import _positions


_MONEY = {"total_equity", "settle_balance", "initial_margin", "maintenance_margin", "unrealized_pnl"}


def _safe(value):
    if isinstance(value,Decimal): return format(value,"f")
    if isinstance(value,(list,tuple)): return [_safe(item) for item in value]
    if isinstance(value,dict): return {key:_safe(item) for key,item in value.items()}
    return value


def make_oco_preflight(plan: dict, state: GrvtTestnetRiskState, rules: MarginRules,
                        quote: GrvtTestnetQuote, history: dict, *, checked_at_ns: int) -> dict:
    return {"schema":"orbis.grvt.testnet.oco.preflight.v1", "group_id":plan["group_id"],
        "parent_receipt_digest":plan["parent_receipt_digest"],"checked_at_ns":checked_at_ns,
        "state":_safe(asdict(state)),"rules":rules.json(),"quote":_safe(asdict(quote)),
        "history_continuity":history}


def _signed(value: object) -> Decimal:
    if not isinstance(value,str) or not re.fullmatch(r"-?[0-9]{1,30}(?:\.[0-9]{1,18})?",value):
        raise ValueError("invalid money")
    return Decimal(value)


def verify_oco_preflight(db, plan: dict, proof: object, *, now_ns: int, historical: bool = False) -> None:
    """Las proyecciones de riesgo no son órdenes firmables ni autoridad mainnet."""
    try:
        if (not isinstance(proof,dict) or set(proof) != {"schema","group_id","parent_receipt_digest",
                "checked_at_ns","state","rules","quote","history_continuity"}
                or proof["schema"] != "orbis.grvt.testnet.oco.preflight.v1"
                or proof["group_id"] != plan["group_id"]
                or proof["parent_receipt_digest"] != plan["parent_receipt_digest"]
                or type(proof["checked_at_ns"]) is not int or type(now_ns) is not int
                or not plan["created_at_ns"] <= proof["checked_at_ns"] <= now_ns
                or now_ns-proof["checked_at_ns"] > 5_000_000_000):
            raise ValueError("invalid preflight")
        source,digest = _parent(db,plan["parent_client_order_id"])
        if digest != proof["parent_receipt_digest"]:
            raise ValueError("source changed")
        raw = proof["state"]
        if (not isinstance(raw,dict) or set(raw) != set(GrvtTestnetRiskState.__dataclass_fields__)
                or any(type(raw[key]) is not int for key in ("event_time_ns","observed_at_ns","open_order_count"))
                or _positions(raw["positions"]) != _positions(source["result"]["positions"])
                or _signed(raw["settle_balance"]) != Decimal(source["result"]["settle_balance"])):
            raise ValueError("position/cash mismatch")
        state = GrvtTestnetRiskState(**{**raw,**{key:_signed(raw[key]) for key in _MONEY},
            "positions":tuple((name,_signed(size)) for name,size in raw["positions"])})
        if state.initial_margin < 0 or state.maintenance_margin < 0:
            raise ValueError("negative margin")
        limits = GrvtTestnetLimits(plan["sub_account_id"],plan["instrument"],_number(plan["max_order_notional"]))
        rules = MarginRules.from_payload(proof["rules"],limits.instrument,now_ns)
        q = proof["quote"]
        if not isinstance(q,dict) or set(q) != set(GrvtTestnetQuote.__dataclass_fields__):
            raise ValueError("quote fields")
        quote = GrvtTestnetQuote(**{**q,**{key:_number(q[key]) for key in
            ("bid","ask","bid_size","ask_size","last_price")}})
        if quote.instrument != limits.instrument:
            raise ValueError("quote scope")
        guard_testnet_limit_price(plan["side"],quote.ask if plan["side"] == "BUY" else quote.bid,
            quote,max_spread_bps=100,max_slippage_bps=0,now_ns=now_ns)
        size = _number(plan["size"])
        with localcontext() as ctx:
            ctx.prec = 100
            if size*quote.last_price > limits.max_order_notional:
                raise ValueError("live position cap")
            for leg in plan["legs"]:
                trigger = _number(leg["trigger_price"])
                higher = (plan["side"] == "BUY") == (leg["role"] == "STOP_LOSS")
                if (trigger <= quote.last_price if higher else trigger >= quote.last_price):
                    raise ValueError("trigger already met")
                # Sólo una proyección de los campos usados por el cálculo de
                # riesgo. Builder cero no sustituye el hash de la orden real.
                draft = GrvtTestnetLimitDraft(limits.sub_account_id,limits.instrument,plan["side"],size,
                    _number(leg["limit_price"]),True,"0x"+"0"*40,leg["client_order_id"],leg["nonce"],leg["expiration_ns"])
                if not evaluate_testnet_risk(draft,limits,state,rules,now_ns=now_ns).allowed:
                    raise ValueError("risk blocked")
        validate_testnet_history_continuity(db,limits.sub_account_id,proof["history_continuity"],
            now_ns=now_ns,account_event_time_ns=state.event_time_ns,
            client_order_id=plan["legs"][0]["client_order_id"],historical=historical)
    except Exception:
        raise GrvtTestnetOcoError("Preflight OCO no verificable") from None
