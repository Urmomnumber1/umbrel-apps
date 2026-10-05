"""Turning proposals into orders. Every path re-checks the kill switch and risk rules with fresh data."""
import os
from datetime import datetime, timezone

import httpx

from . import db, risk
from .broker import Alpaca, BrokerError


def broker_from_settings(s: dict) -> Alpaca:
    if not s["alpaca_paper"] and not live_allowed():
        raise BrokerError("Live trading is locked. Set ALLOW_LIVE_TRADING=1 in the app's environment to unlock.")
    return Alpaca(s["alpaca_key_id"], s["alpaca_secret"], paper=s["alpaca_paper"])


def live_allowed() -> bool:
    return os.environ.get("ALLOW_LIVE_TRADING") == "1"


def trades_today() -> int:
    return db.executed_today_count(datetime.now(timezone.utc).date().isoformat())


def evaluate(p: dict, s: dict, broker: Alpaca) -> list[str]:
    return risk.check(p, s, broker.account(), broker.positions(), broker.clock(), trades_today())


def execute(pid: int) -> dict:
    """Execute a stored proposal (pending, or freshly created in auto mode)."""
    p = db.get_proposal(pid)
    if not p:
        return {"ok": False, "error": "Proposal not found."}
    s = db.get_settings()
    if not s["trading_enabled"]:
        return {"ok": False, "error": "Trading is disabled (kill switch). Proposal left as-is."}
    try:
        broker = broker_from_settings(s)
        problems = evaluate(p, s, broker)
        if problems:
            db.set_proposal(pid, "rejected_by_risk", "; ".join(problems))
            return {"ok": False, "error": "; ".join(problems)}
        order = broker.submit_market_order(p["symbol"], p["side"], notional=p["notional_usd"], qty=p["qty"])
    except (BrokerError, httpx.HTTPError) as e:
        db.set_proposal(pid, "failed", str(e))
        return {"ok": False, "error": str(e)}
    db.set_proposal(pid, "executed", order_id=order["id"])
    return {"ok": True, "order": order}


def expire_stale() -> None:
    """Pending proposals are only valid for the day they were made."""
    today = datetime.now(timezone.utc).date().isoformat()
    for p in db.query("SELECT id FROM proposals WHERE status='pending' AND created_at < ?", (today,)):
        db.set_proposal(p["id"], "expired")
