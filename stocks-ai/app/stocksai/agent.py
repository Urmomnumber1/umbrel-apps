"""One AI decision cycle: Claude reviews the portfolio with read-only tools and may call
propose_trade. Proposals go through the risk gate in code; Claude cannot place orders directly."""
import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
import httpx

from . import db, trader
from .broker import BrokerError

MAX_TURNS = 20
MAX_PROPOSALS_PER_RUN = 5
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

_run_lock = threading.Lock()

SYSTEM_PROMPT = """You are the portfolio manager for a small personal brokerage account. Each session you \
review the account, positions, prices and news, then decide whether any trades are warranted.

How you work:
- Gather what you need with the read-only tools before deciding.
- Follow the investor's strategy. When there's no clear reason to trade, don't trade. Inaction is a good outcome.
- Use propose_trade for each trade you want. Buys are sized in dollars (notional_usd); sells in shares (qty).
- Hard risk limits are enforced by code. A proposal outside them is rejected and you'll be told why; adjust or move on.
- Depending on the mode, proposals either wait for human approval or execute immediately. Either way, give a \
clear, specific reason the investor can evaluate.
- Market data comes from a free feed and can be delayed or incomplete. Account for that.
- Finish with a short plain-English summary: what you looked at, what you proposed, and why."""

TOOLS = [
    {
        "name": "get_account",
        "description": "Account equity, cash, buying power, and today's P/L versus yesterday's close.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_positions",
        "description": "All current positions with quantity, cost basis, market value, and unrealized P/L.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_quotes",
        "description": "Latest price, previous close, and day change % for up to 30 symbols.",
        "input_schema": {
            "type": "object",
            "properties": {"symbols": {"type": "array", "items": {"type": "string"}, "maxItems": 30}},
            "required": ["symbols"],
        },
    },
    {
        "name": "get_price_history",
        "description": "Daily closing prices and volume for one symbol over the last N trading days (max 250).",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}, "days": {"type": "integer", "minimum": 5, "maximum": 250}},
            "required": ["symbol", "days"],
        },
    },
    {
        "name": "get_news",
        "description": "Recent news headlines and summaries for the given symbols.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            "required": ["symbols"],
        },
    },
    {
        "name": "propose_trade",
        "description": (
            "Propose one market order. Buys: set notional_usd (dollars), omit qty. "
            "Sells: set qty (shares, fractional allowed), omit notional_usd. "
            "Returns whether it was queued for approval, executed, or rejected by risk rules."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "notional_usd": {"type": "number"},
                "qty": {"type": "number"},
                "reason": {"type": "string", "description": "Specific rationale the investor can evaluate."},
            },
            "required": ["symbol", "side", "reason"],
        },
    },
]


def _limits_text(s: dict) -> str:
    return (
        f"- Max order size: ${s['max_order_usd']:,.0f}\n"
        f"- Max single position: {s['max_position_pct']}% of equity\n"
        f"- Keep at least {s['min_cash_pct']}% of equity in cash\n"
        f"- Max {s['max_trades_per_day']} executed trades per day\n"
        f"- Buys pause if the portfolio is down {s['max_daily_loss_pct']}% on the day\n"
        f"- No shorting, no options, no margin; market orders only"
        + ("\n- Only watchlist symbols (or ones already held)" if s["restrict_to_watchlist"] else "")
    )


class Cycle:
    def __init__(self, run_id: int, s: dict):
        self.run_id = run_id
        self.s = s
        self.broker = trader.broker_from_settings(s)
        self.proposals = 0

    def call_tool(self, name: str, args: dict) -> str:
        b = self.broker
        if name == "get_account":
            a = b.account()
            a["day_pl_pct"] = round((a["equity"] - a["last_equity"]) / a["last_equity"] * 100, 2) if a["last_equity"] else None
            return json.dumps(a)
        if name == "get_positions":
            return json.dumps(b.positions())
        if name == "get_quotes":
            return json.dumps(b.quotes([x.upper() for x in args["symbols"]][:30]))
        if name == "get_price_history":
            return json.dumps(b.daily_bars(args["symbol"].upper(), min(int(args["days"]), 250)))
        if name == "get_news":
            return json.dumps(b.news([x.upper() for x in args["symbols"]][:10], int(args.get("limit", 10))))
        if name == "propose_trade":
            return self.propose(args)
        raise ValueError(f"Unknown tool {name}")

    def propose(self, args: dict) -> str:
        if self.proposals >= MAX_PROPOSALS_PER_RUN:
            return "Proposal limit for this session reached. Do not propose more trades; write your summary."
        self.proposals += 1
        p = {
            "symbol": str(args.get("symbol", "")).upper().strip(),
            "side": args.get("side"),
            "notional_usd": args.get("notional_usd"),
            "qty": args.get("qty"),
        }
        reason = str(args.get("reason", ""))[:2000]
        problems = trader.evaluate(p, self.s, self.broker)
        if problems:
            db.add_proposal(self.run_id, p["symbol"], p["side"] or "?", p["notional_usd"], p["qty"], reason,
                            "rejected_by_risk", "; ".join(problems))
            return "REJECTED by risk rules: " + "; ".join(problems)
        pid = db.add_proposal(self.run_id, p["symbol"], p["side"], p["notional_usd"], p["qty"], reason, "pending")
        if self.s["mode"] == "auto" and self.s["trading_enabled"]:
            result = trader.execute(pid)
            if result["ok"]:
                return f"EXECUTED: order {result['order']['id']} ({result['order']['status']})."
            return f"NOT EXECUTED: {result['error']}"
        return f"QUEUED as proposal #{pid}; it will only execute if the investor approves it."


def run_cycle(trigger: str = "manual") -> dict:
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "A run is already in progress."}
    try:
        return _run(trigger)
    finally:
        _run_lock.release()


def _run(trigger: str) -> dict:
    s = db.get_settings()
    if not s["anthropic_api_key"]:
        return {"ok": False, "error": "Anthropic API key is not set."}
    run_id = db.start_run(trigger)
    in_tok = out_tok = 0
    try:
        cycle = Cycle(run_id, s)
        clock = cycle.broker.clock()
        now_et = datetime.now(ZoneInfo("America/New_York")).strftime("%A %Y-%m-%d %H:%M ET")
        mode_text = (
            "AUTO: proposals that pass the risk rules execute immediately."
            if s["mode"] == "auto" and s["trading_enabled"]
            else "APPROVAL: proposals are queued for the investor to approve or reject."
        )
        user_msg = (
            f"Session time: {now_et}. Market open: {clock['is_open']} (next open {clock['next_open']}, "
            f"next close {clock['next_close']}).\n"
            f"Account type: {'paper' if s['alpaca_paper'] else 'LIVE'}. Mode: {mode_text}\n\n"
            f"Watchlist: {', '.join(s['watchlist'])}\n\n"
            f"<investor_strategy>\n{s['strategy']}\n</investor_strategy>\n\n"
            f"Risk limits (enforced by code):\n{_limits_text(s)}\n\n"
            "Review the portfolio and decide whether any trades are warranted."
        )
        messages = [{"role": "user", "content": user_msg}]
        client = anthropic.Anthropic(api_key=s["anthropic_api_key"])

        params = dict(
            model=s["model"],
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            cache_control={"type": "ephemeral"},
        )
        if not s["model"].startswith("claude-haiku"):
            params["thinking"] = {"type": "adaptive"}
            params["output_config"] = {"effort": s["effort"]}
        if s["model"] in FALLBACK_MODELS:
            params["betas"] = ["server-side-fallback-2026-07-01"]
            params["fallbacks"] = "default"

        summary, status = "", "completed"
        for _ in range(MAX_TURNS):
            resp = client.beta.messages.create(messages=messages, **params)
            in_tok += resp.usage.input_tokens + (resp.usage.cache_read_input_tokens or 0) + (resp.usage.cache_creation_input_tokens or 0)
            out_tok += resp.usage.output_tokens

            if resp.stop_reason == "refusal":
                status, summary = "refused", "The model declined this request."
                db.log_event(run_id, "error", summary)
                break

            texts = [b.text for b in resp.content if b.type == "text" and b.text.strip()]
            for t in texts:
                db.log_event(run_id, "assistant", t)

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                summary = texts[-1] if texts else ""
                if resp.stop_reason == "max_tokens":
                    status = "truncated"
                break

            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for tu in tool_uses:
                db.log_event(run_id, "tool_call", {"name": tu.name, "input": tu.input})
                try:
                    out = cycle.call_tool(tu.name, tu.input)
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
                except (BrokerError, httpx.HTTPError, KeyError, ValueError, TypeError) as e:
                    out = f"Error: {e}"
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out, "is_error": True})
                db.log_event(run_id, "tool_result", {"name": tu.name, "output": out[:4000]})
            messages.append({"role": "user", "content": results})
        else:
            status, summary = "turn_limit", "Stopped after reaching the turn limit."

        db.finish_run(run_id, status, summary, in_tok, out_tok)
        return {"ok": True, "run_id": run_id, "status": status}
    except (anthropic.APIError, BrokerError, httpx.HTTPError) as e:
        db.log_event(run_id, "error", str(e))
        db.finish_run(run_id, "error", str(e), in_tok, out_tok)
        return {"ok": False, "run_id": run_id, "error": str(e)}
