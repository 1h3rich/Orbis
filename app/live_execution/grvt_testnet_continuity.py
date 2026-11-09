"""Historial entre dos intentos TESTNET; una fotografía no prueba continuidad."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.grvt_testnet_evidence import _verified_payload
from app.live_execution.grvt_testnet_fills import audit_testnet_fills
from app.live_execution.grvt_testnet_funding import audit_testnet_funding
from app.live_execution.grvt_testnet_transfers import audit_testnet_transfers
from app.live_execution.grvt_testnet_withdrawals import audit_testnet_withdrawals
from app.live_execution.grvt_testnet_deposits import audit_testnet_deposits
from app.live_execution.grvt_testnet_interest import audit_testnet_interest
from app.live_execution.grvt_testnet_preflight import _verified
from app.live_execution.grvt_testnet_reconciliation import (
    _ns, require_reconciled_continuity, verified_reconciliation,
)
from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskError
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient
from app.portfolio.vault import RealVault
from app.portfolio.models import (
    GrvtTestnetAuditEvidence, GrvtTestnetOrderIntent,
    GrvtTestnetPreflightEvidence, GrvtTestnetReconciliation,
)


_verifying: ContextVar[tuple[str, ...]] = ContextVar("grvt_history_receipts", default=())
_HISTORIES = (
    ("funding", audit_testnet_funding, "payments"),
    ("transfers", audit_testnet_transfers, "transfers"),
    ("withdrawals", audit_testnet_withdrawals, "withdrawals"),
    ("deposits", audit_testnet_deposits, "deposits"),
    ("interest", audit_testnet_interest, "payments"),
)


@dataclass(frozen=True, slots=True)
class _Anchor:
    client_order_id: str
    receipt_digest: str
    history_start_ns: int
    account_event_time_ns: int
    order: dict
    fills: list[dict]
    funding: list[dict]
    oco_group_id: str | None = None
    oco_orders: list[dict] | None = None
    bracket_parent_id: str | None = None
    bracket_orders: list[dict] | None = None


def _anchor(db: Session, sub_account_id: str, *, now_ns: int,
            client_order_id: str | None = None, historical: bool = False) -> _Anchor | None:
    receipts = db.scalars(select(GrvtTestnetReconciliation).where(
        GrvtTestnetReconciliation.sub_account_id == sub_account_id).order_by(
            GrvtTestnetReconciliation.reconciled_at.desc(),
            GrvtTestnetReconciliation.client_order_id.desc())).all()
    if historical:
        receipts = [row for row in receipts if row.client_order_id != client_order_id
                    and _ns(row.reconciled_at) <= now_ns]
    from app.live_execution.grvt_testnet_oco_continuity import oco_receipts, oco_anchor_data
    native = oco_receipts(db,sub_account_id)
    if historical:
        native = [pair for pair in native if int(pair[0].reconciled_at_ns) <= now_ns]
    from app.live_execution.grvt_testnet_bracket_continuity import bracket_receipts,bracket_anchor_data
    brackets=bracket_receipts(db,sub_account_id)
    if historical: brackets=[pair for pair in brackets if int(pair[0].reconciled_at_ns)<=now_ns]
    newest=max(_ns(receipts[0].reconciled_at) if receipts else 0,int(native[-1][0].reconciled_at_ns) if native else 0)
    if brackets and int(brackets[-1][0].reconciled_at_ns)>newest:
        row,group=brackets[-1]; key='bracket:'+row.parent_client_order_id; stack=_verifying.get()
        if int(row.reconciled_at_ns)>now_ns or key in stack or len(stack)>=128:
            raise ValueError('invalid OTOCO lineage')
        token=_verifying.set(stack+(key,))
        try: return _Anchor(**bracket_anchor_data(db,row,group))
        finally: _verifying.reset(token)
    if native and (not receipts or int(native[-1][0].reconciled_at_ns) > _ns(receipts[0].reconciled_at)):
        row,group = native[-1]
        key = 'oco:'+row.group_id
        stack = _verifying.get()
        if int(row.reconciled_at_ns) > now_ns or key in stack or len(stack) >= 128:
            raise ValueError('invalid OCO lineage')
        token = _verifying.set(stack+(key,))
        try:
            return _Anchor(**oco_anchor_data(db,row,group))
        finally:
            _verifying.reset(token)
    if not receipts:
        return None
    receipt = receipts[0]
    stack = _verifying.get()
    if (_ns(receipt.reconciled_at) > now_ns or receipt.client_order_id in stack
            or len(stack) >= 128):
        raise ValueError("invalid receipt lineage")
    token = _verifying.set(stack + (receipt.client_order_id,))
    try:
        previous = db.get(GrvtTestnetOrderIntent, receipt.client_order_id)
        if previous is None:
            raise ValueError("missing previous intent")
        payload = verified_reconciliation(db, previous)
        baseline = _verified_payload(db.get(GrvtTestnetAuditEvidence, payload["audits"][-1][0]))
        start = _verified(db.get(GrvtTestnetPreflightEvidence, receipt.client_order_id))["observed_at_ns"]
        return _Anchor(receipt.client_order_id, receipt.payload_digest, start,
                       payload["account_event_time_ns"], baseline["order"], baseline["fills"]["rows"],
                       baseline["funding"]["rows"])
    finally:
        _verifying.reset(token)


def validate_testnet_history_continuity(db: Session, sub_account_id: str, proof: object, *,
                                       now_ns: int, account_event_time_ns: int,
                                       client_order_id: str, historical: bool = False) -> None:
    """Revalida el recibo exacto bajo la transacción que reserva/reclama."""
    try:
        anchor = _anchor(db, sub_account_id, now_ns=now_ns,
                         client_order_id=client_order_id, historical=historical)
        if anchor is None:
            if proof is not None:
                raise ValueError("unexpected history proof")
            return
        previous_key = 'previous_bracket_parent_id' if anchor.bracket_parent_id else 'previous_group_id' if anchor.oco_group_id else 'previous_client_order_id'
        fields = {"schema", previous_key, "reconciliation_digest",
                  "history_start_ns", "observed_until_ns", "fills", "empty_histories"}
        if anchor.funding:
            fields.add("funding")
        history_names = {name for name,_,_ in _HISTORIES} - ({"funding"} if anchor.funding else set())
        if (not isinstance(proof, dict) or set(proof) != fields
                or proof["schema"] != ('orbis.grvt.testnet.history.v4' if anchor.bracket_parent_id else 'orbis.grvt.testnet.history.v3' if anchor.oco_group_id else
                    "orbis.grvt.testnet.history.v2" if anchor.funding else "orbis.grvt.testnet.history.v1")
                or proof[previous_key] != (anchor.bracket_parent_id or anchor.oco_group_id or anchor.client_order_id)
                or proof["reconciliation_digest"] != anchor.receipt_digest
                or type(proof["history_start_ns"]) is not int
                or proof["history_start_ns"] != anchor.history_start_ns
                or type(proof["observed_until_ns"]) is not int
                or not anchor.account_event_time_ns <= proof["observed_until_ns"] <= now_ns
                or now_ns - proof["observed_until_ns"] > 5_000_000_000
                or type(account_event_time_ns) is not int
                or account_event_time_ns < proof["observed_until_ns"]
                or not isinstance(proof["fills"], dict)
                or set(proof["fills"]) != {"rows", "pages_drained"}
                or proof["fills"]["rows"] != anchor.fills
                or not isinstance(proof["empty_histories"], dict)
                or set(proof["empty_histories"]) != history_names):
            raise ValueError("history proof mismatch")
        funding = proof.get("funding")
        if anchor.funding and (not isinstance(funding,dict) or set(funding) != {"rows","pages_drained"}
                               or funding["rows"] != anchor.funding):
            raise ValueError("previous funding changed")
        for section in (proof["fills"], *proof["empty_histories"].values(), *((funding,) if anchor.funding else ())):
            if (not isinstance(section, dict) or type(section.get("pages_drained")) is not int
                    or not 1 <= section["pages_drained"] <= 20):
                raise ValueError("unverified pages")
        if any(set(section) != {"rows", "pages_drained"} or section["rows"] != []
               for section in proof["empty_histories"].values()):
            raise ValueError("unexpected history activity")
    except Exception:
        raise GrvtTestnetRiskError("Historial entre operaciones GRVT testnet no verificable; revisa la subcuenta") from None


def check_testnet_history_continuity(vault: RealVault, client: GrvtTestnetOrderClient,
                                     sub_account_id: str, *, now_ns: int) -> dict | None:
    """Agota seis historiales desde el preflight previo, sin enviar órdenes.

    El corte queda fijado antes de la red. La siguiente lectura de cuenta debe
    ser posterior; esta evidencia no demuestra atomicidad ni omisiones del venue.
    """
    try:
        with vault.session() as db:
            require_reconciled_continuity(db, sub_account_id, now_ns=now_ns)
            anchor = _anchor(db, sub_account_id, now_ns=now_ns)
            if anchor is None:
                return None
        if now_ns < anchor.account_event_time_ns:
            raise ValueError("clock before receipt")
        if anchor.bracket_parent_id:
            from app.live_execution.grvt_testnet_bracket_continuity import read_bracket_anchor_fills
            rows,pages=read_bracket_anchor_fills(vault,client,sub_account_id,anchor,now_ns=now_ns)
        elif anchor.oco_group_id:
            from app.live_execution.grvt_testnet_oco_continuity import read_oco_anchor_fills
            rows,pages = read_oco_anchor_fills(vault,client,sub_account_id,anchor,now_ns=now_ns)
        else:
            order = client.lookup_order(sub_account_id, anchor.client_order_id)
            if any(getattr(order, key) != value for key, value in anchor.order.items()):
                raise ValueError("previous order changed")
            fills = audit_testnet_fills(client, order, start_time_ns=anchor.history_start_ns,
                                    now_ns=now_ns, end_time_ns=now_ns)
            rows = [{key:format(value,"f") if isinstance(value,Decimal) else value
                 for key,value in asdict(fill).items()} for fill in fills.fills]
            if fills.other_fills or not fills.sizes_match or rows != anchor.fills:
                raise ValueError("unexplained fills")
            pages = fills.pages_drained
        histories = {}
        known_funding = None
        for name, reader, field in _HISTORIES:
            audit = reader(client,sub_account_id,start_time_ns=anchor.history_start_ns,now_ns=now_ns)
            if name == "funding":
                funding_rows = [{key:format(value,"f") if isinstance(value,Decimal) else value
                                 for key,value in asdict(payment).items()} for payment in audit.payments]
                if anchor.oco_group_id or anchor.bracket_parent_id:
                    from app.live_execution.grvt_testnet_oco_finance import canonical_funding_rows
                    funding_rows = canonical_funding_rows(funding_rows)
                if funding_rows != anchor.funding:
                    raise ValueError("previous funding changed")
                if anchor.funding:
                    known_funding = {"pages_drained":audit.pages_drained,"rows":funding_rows}
                    continue
            if getattr(audit,field) or getattr(audit,"unrelated_row_count",0):
                raise ValueError("unexplained movements")
            histories[name] = {"pages_drained":audit.pages_drained,"rows":[]}
        return {"schema":('orbis.grvt.testnet.history.v4' if anchor.bracket_parent_id else 'orbis.grvt.testnet.history.v3' if anchor.oco_group_id else
                          "orbis.grvt.testnet.history.v2" if known_funding else "orbis.grvt.testnet.history.v1"),
                ('previous_bracket_parent_id' if anchor.bracket_parent_id else 'previous_group_id' if anchor.oco_group_id else 'previous_client_order_id'):anchor.bracket_parent_id or anchor.oco_group_id or anchor.client_order_id,
                "reconciliation_digest":anchor.receipt_digest,
                "history_start_ns":anchor.history_start_ns,"observed_until_ns":now_ns,
                "fills":{"rows":rows,"pages_drained":pages},
                "empty_histories":histories,
                **({"funding":known_funding} if known_funding else {})}
    except Exception:
        raise GrvtTestnetRiskError("Historial entre operaciones GRVT testnet no verificable; revisa la subcuenta") from None
