"""Offline tests: risk rules, plus one full agent cycle with a fake broker and a scripted fake Claude."""
import json
import os
import sys
import tempfile
from types import SimpleNamespace as NS

os.environ["STOCKSAI_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from stocksai import agent, db, risk, trader  # noqa: E402

S = {**db.DEFAULT_SETTINGS}
ACCT = {"equity": 10000.0, "last_equity": 10000.0, "cash": 5000.0, "buying_power": 5000.0, "trading_blocked": False}
POS = [{"symbol": "SPY", "qty": 3.0, "market_value": 1500.0}]
OPEN = {"is_open": True, "next_open": "", "next_close": ""}


def chk(p, s=S, acct=ACCT, clock=OPEN, n=0):
    return risk.check(p, s, acct, POS, clock, n)


def test_risk_rules():
    assert chk({"symbol": "AAPL", "side": "buy", "notional_usd": 300}) == []
    assert any("max order" in x for x in chk({"symbol": "AAPL", "side": "buy", "notional_usd": 900}))
    assert any("watchlist" in x for x in chk({"symbol": "GME", "side": "buy", "notional_usd": 100}))
    assert any("closed" in x for x in chk({"symbol": "AAPL", "side": "buy", "notional_usd": 100}, clock={"is_open": False}))
    assert any("% of the portfolio" in x for x in chk({"symbol": "SPY", "side": "buy", "notional_usd": 500}, s={**S, "max_position_pct": 15}))
    assert any("reserve" in x for x in chk({"symbol": "AAPL", "side": "buy", "notional_usd": 450}, acct={**ACCT, "cash": 1400}))
    assert any("down" in x for x in chk({"symbol": "AAPL", "side": "buy", "notional_usd": 100}, acct={**ACCT, "equity": 9600}))
    assert any("limit reached" in x for x in chk({"symbol": "AAPL", "side": "buy", "notional_usd": 100}, n=5))
    assert chk({"symbol": "SPY", "side": "sell", "qty": 2}) == []
    assert any("shorting" in x for x in chk({"symbol": "SPY", "side": "sell", "qty": 5}))
    assert any("No AAPL position" in x for x in chk({"symbol": "AAPL", "side": "sell", "qty": 1}))
    assert chk({"symbol": "SPY", "side": "sell", "qty": 2}, acct={**ACCT, "equity": 9000}) == []  # sells allowed on bad days
    assert any("Side" in x for x in chk({"symbol": "SPY", "side": "short", "qty": 1}))


class FakeBroker:
    orders = []
    def account(self): return dict(ACCT)
    def positions(self): return [dict(p, current_price=500, avg_entry_price=450, unrealized_pl=150, unrealized_plpc=11.1) for p in POS]
    def clock(self): return dict(OPEN)
    def quotes(self, syms): return {s: {"price": 100.0, "prev_close": 99.0, "change_pct": 1.01} for s in syms}
    def daily_bars(self, sym, days): return [{"date": "2026-10-01", "close": 100.0, "volume": 1}]
    def news(self, syms, limit=10): return []
    def submit_market_order(self, symbol, side, notional=None, qty=None):
        self.orders.append((symbol, side, notional, qty)); return {"id": f"ord-{len(self.orders)}", "status": "accepted"}


def tool_use(id_, name, inp): return NS(type="tool_use", id=id_, name=name, input=inp)
def resp(content, stop): return NS(content=content, stop_reason=stop, usage=NS(input_tokens=100, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0))


class FakeClient:
    """Scripted Claude: looks at the account, proposes a valid buy and an oversized one, then summarizes."""
    def __init__(self, **kw):
        self.calls = []
        script = [
            resp([NS(type="thinking", thinking=""), tool_use("t1", "get_account", {}), tool_use("t2", "get_quotes", {"symbols": ["aapl"]})], "tool_use"),
            resp([tool_use("t3", "propose_trade", {"symbol": "aapl", "side": "buy", "notional_usd": 250, "reason": "Adding to quality."}),
                  tool_use("t4", "propose_trade", {"symbol": "NVDA", "side": "buy", "notional_usd": 5000, "reason": "Too big."})], "tool_use"),
            resp([NS(type="text", text="Proposed one AAPL buy; NVDA was rejected for size.")], "end_turn"),
        ]
        outer = self
        class Msgs:
            def create(self, **params):
                outer.calls.append(json.loads(json.dumps(params, default=lambda o: getattr(o, "__dict__", str(o)))))
                return script[len(outer.calls) - 1]
        self.beta = NS(messages=Msgs())


def run_with_fakes(monkeypatch, **settings):
    db.update_settings({"anthropic_api_key": "test", "alpaca_key_id": "k", "alpaca_secret": "s", **settings})
    broker = FakeBroker()
    clients = []
    monkeypatch.setattr(trader, "broker_from_settings", lambda s: broker)
    monkeypatch.setattr(agent.anthropic, "Anthropic", lambda **kw: clients.append(FakeClient(**kw)) or clients[-1])
    result = agent.run_cycle("test")
    return result, broker, clients[0]


def test_agent_cycle_approve_mode(monkeypatch):
    FakeBroker.orders = []
    result, broker, client = run_with_fakes(monkeypatch, mode="approve", trading_enabled=True)
    assert result["ok"] and result["status"] == "completed"
    props = db.query("SELECT * FROM proposals WHERE run_id=? ORDER BY id", (result["run_id"],))
    assert [(p["symbol"], p["status"]) for p in props] == [("AAPL", "pending"), ("NVDA", "rejected_by_risk")]
    assert broker.orders == []  # approve mode: nothing executes on its own
    # request shape
    first = client.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["thinking"] == {"type": "adaptive"}
    assert first["betas"] == ["server-side-fallback-2026-07-01"] and first["fallbacks"] == "default"
    # tool results for parallel calls come back in one user message, and the NVDA rejection is explained
    last_user = client.calls[2]["messages"][-1]["content"]
    assert len(last_user) == 2 and "REJECTED" in last_user[1]["content"] and "QUEUED" in last_user[0]["content"]
    # approving executes; approving again is refused upstream (status no longer pending)
    assert trader.execute(props[0]["id"])["ok"]
    assert broker.orders == [("AAPL", "buy", 250.0, None)]
    assert db.get_proposal(props[0]["id"])["status"] == "executed"


def test_kill_switch_blocks_execution(monkeypatch):
    FakeBroker.orders = []
    result, broker, _ = run_with_fakes(monkeypatch, mode="auto", trading_enabled=False)
    pid = db.query("SELECT id FROM proposals WHERE run_id=? AND symbol='AAPL'", (result["run_id"],))[0]["id"]
    assert broker.orders == [] and db.get_proposal(pid)["status"] == "pending"
    assert not trader.execute(pid)["ok"]


def test_auto_mode_executes(monkeypatch):
    FakeBroker.orders = []
    result, broker, _ = run_with_fakes(monkeypatch, mode="auto", trading_enabled=True)
    assert broker.orders == [("AAPL", "buy", 250.0, None)]
