"""Web server: dashboard, settings, approvals, and the background scheduler."""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import agent, db, trader
from .broker import BrokerError

log = logging.getLogger("stocksai")
STATIC = Path(__file__).parent / "static"


async def scheduler():
    """Every minute: expire stale proposals; start a run if scheduling is on, the market is open, and the interval has passed."""
    while True:
        try:
            await asyncio.to_thread(trader.expire_stale)
            s = db.get_settings()
            if s["schedule_enabled"] and s["anthropic_api_key"] and s["alpaca_key_id"]:
                last = db.query("SELECT started_at FROM runs WHERE trigger='schedule' ORDER BY id DESC LIMIT 1")
                due = not last or datetime.fromisoformat(last[0]["started_at"]) < datetime.now(timezone.utc) - timedelta(
                    minutes=s["interval_minutes"]
                )
                if due:
                    clock = await asyncio.to_thread(lambda: trader.broker_from_settings(s).clock())
                    if clock["is_open"]:
                        await asyncio.to_thread(agent.run_cycle, "schedule")
        except Exception:  # keep the scheduler alive no matter what
            log.exception("scheduler tick failed")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    db.execute("UPDATE runs SET status='interrupted', finished_at=? WHERE status='running'", (db.now_iso(),))
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()


app = FastAPI(title="Stocks AI", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state():
    s = db.get_settings()
    out = {"settings": db.public_settings(), "live_allowed": trader.live_allowed(), "broker_error": None}
    try:
        b = trader.broker_from_settings(s)
        out.update(account=b.account(), positions=b.positions(), clock=b.clock())
    except (BrokerError, httpx.HTTPError) as e:
        out["broker_error"] = str(e)
    out["pending"] = db.query("SELECT * FROM proposals WHERE status='pending' ORDER BY id DESC")
    out["trades_today"] = trader.trades_today()
    return out


@app.post("/api/settings")
def save_settings(changes: dict):
    changes = {k: v for k, v in changes.items() if k in db.DEFAULT_SETTINGS}
    for key in db.SECRET_KEYS:  # blank secret fields mean "keep the current value"
        if key in changes and not changes[key]:
            del changes[key]
    if changes.get("alpaca_paper") is False and not trader.live_allowed():
        raise HTTPException(400, "Live trading is locked. Set ALLOW_LIVE_TRADING=1 in the app environment first.")
    if "mode" in changes and changes["mode"] not in ("approve", "auto"):
        raise HTTPException(400, "mode must be 'approve' or 'auto'")
    if "watchlist" in changes:
        changes["watchlist"] = sorted({w.strip().upper() for w in changes["watchlist"] if w.strip()})
    db.update_settings(changes)
    return db.public_settings()


@app.post("/api/kill")
def kill():
    db.update_settings({"trading_enabled": False, "schedule_enabled": False})
    return {"ok": True}


@app.post("/api/run")
async def run_now():
    if agent._run_lock.locked():
        raise HTTPException(409, "A run is already in progress.")
    s = db.get_settings()
    if not (s["anthropic_api_key"] and s["alpaca_key_id"] and s["alpaca_secret"]):
        raise HTTPException(400, "Add your Anthropic and Alpaca keys in Settings first.")
    asyncio.get_running_loop().run_in_executor(None, agent.run_cycle, "manual")
    return {"ok": True}


@app.get("/api/runs")
def runs():
    return db.query("SELECT * FROM runs ORDER BY id DESC LIMIT 50")


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int):
    return {
        "events": db.query("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)),
        "proposals": db.query("SELECT * FROM proposals WHERE run_id=? ORDER BY id", (run_id,)),
    }


@app.get("/api/proposals")
def proposals():
    return db.query("SELECT * FROM proposals ORDER BY id DESC LIMIT 100")


@app.post("/api/proposals/{pid}/approve")
def approve(pid: int):
    p = db.get_proposal(pid)
    if not p or p["status"] != "pending":
        raise HTTPException(400, "Only pending proposals can be approved.")
    return trader.execute(pid)


@app.post("/api/proposals/{pid}/reject")
def reject(pid: int):
    p = db.get_proposal(pid)
    if not p or p["status"] != "pending":
        raise HTTPException(400, "Only pending proposals can be rejected.")
    db.set_proposal(pid, "rejected")
    return {"ok": True}
