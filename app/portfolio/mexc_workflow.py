"""Local recovery actions for one linked MEXC account."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.reconciliation_view import PENDING_SPOT_STATUSES
from app.portfolio.mexc_withdrawal_booking import settlement_status as withdrawal_settlement
from app.portfolio.mexc_withdrawal_journal import withdrawal_record
from app.portfolio.models import (
    RealLiveSpotIntent,
    RealMexcWithdrawal,
    RealMexcWithdrawalBooking,
    RealWalletFundingBooking,
    RealWalletFundingIntent,
)
from app.portfolio.wallet_funding import funding_record
from app.portfolio.wallet_funding_credit import settlement_status as funding_settlement


def workflow_actions(db: Session, account_id: str, account_ref: str, *, now: datetime) -> dict[str, str]:
    """Return each next action and the local ID of its transfer."""
    actions: dict[str, str] = {}
    # An attempted external submission takes precedence over an unsent reservation.
    pending = db.scalar(select(RealLiveSpotIntent.status).where(
        RealLiveSpotIntent.account_ref == account_ref,
        RealLiveSpotIntent.status.in_(PENDING_SPOT_STATUSES),
    ).order_by((RealLiveSpotIntent.status == "SUBMISSION_UNKNOWN").desc()).limit(1))
    if pending is not None:
        actions["spot_order_action"] = "SYNCHRONIZE" if pending == "SUBMISSION_UNKNOWN" else "REVIEW_RESERVATION"

    funding = db.scalar(
        select(RealWalletFundingIntent)
        .outerjoin(RealWalletFundingBooking, RealWalletFundingBooking.funding_id == RealWalletFundingIntent.id)
        .where(
            RealWalletFundingIntent.account_id == account_id,
            RealWalletFundingIntent.account_ref == account_ref,
            RealWalletFundingIntent.status.in_(("WALLET_REQUESTED", "HASH_REPORTED")),
            RealWalletFundingBooking.funding_id.is_(None),
        )
        .order_by(RealWalletFundingIntent.created_at.desc(), RealWalletFundingIntent.id.desc())
        .limit(1)
    )
    if funding is not None:
        row, _, _ = funding_record(db, funding.id)
        actions["funding_id"] = row.id
        if row.status == "WALLET_REQUESTED":
            actions["funding_action"] = "SEARCH"
        else:
            settled = funding_settlement(db, row.id)
            actions["funding_action"] = "BOOK" if settled["exchange_credited"] else "CHECK"

    withdrawals = db.scalars(
        select(RealMexcWithdrawal)
        .outerjoin(RealMexcWithdrawalBooking, RealMexcWithdrawalBooking.withdrawal_id == RealMexcWithdrawal.id)
        .where(
            RealMexcWithdrawal.account_id == account_id,
            RealMexcWithdrawal.account_ref == account_ref,
            RealMexcWithdrawal.status.in_(("SUBMISSION_UNKNOWN", "ACKNOWLEDGED", "PREPARED")),
            RealMexcWithdrawalBooking.withdrawal_id.is_(None),
        )
        .order_by(RealMexcWithdrawal.created_at.desc(), RealMexcWithdrawal.id.desc())
    )
    for withdrawal in withdrawals:
        row, terms, _ = withdrawal_record(db, withdrawal.id)
        if row.status == "PREPARED":
            if now >= datetime.fromisoformat(terms["expires_at"]):
                continue
            actions["withdrawal_action"] = "REVIEW_PLAN"
        else:
            settled = withdrawal_settlement(db, row.id)
            if settled["wallet_received"]:
                actions["withdrawal_action"] = "BOOK"
            elif settled.get("can_review_rejected"):
                actions["withdrawal_action"] = "REVIEW_REJECTION"
            else:
                actions["withdrawal_action"] = "CHECK"
        actions["withdrawal_id"] = row.id
        break
    return actions
