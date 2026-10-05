"""Thin Alpaca REST client (trading + market data)."""
from datetime import datetime, timedelta, timezone

import httpx

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"


class BrokerError(Exception):
    pass


class Alpaca:
    def __init__(self, key_id: str, secret: str, paper: bool = True):
        if not key_id or not secret:
            raise BrokerError("Alpaca API keys are not set.")
        headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self.paper = paper
        self.trading = httpx.Client(base_url=PAPER_URL if paper else LIVE_URL, headers=headers, timeout=20)
        self.data = httpx.Client(base_url=DATA_URL, headers=headers, timeout=20)

    @staticmethod
    def _check(resp: httpx.Response):
        if resp.status_code >= 400:
            raise BrokerError(f"Alpaca {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ---- account ----

    def account(self) -> dict:
        a = self._check(self.trading.get("/v2/account"))
        return {
            "equity": float(a["equity"]),
            "last_equity": float(a["last_equity"]),
            "cash": float(a["cash"]),
            "buying_power": float(a["buying_power"]),
            "status": a.get("status"),
            "trading_blocked": a.get("trading_blocked", False),
        }

    def positions(self) -> list[dict]:
        return [
            {
                "symbol": p["symbol"],
                "qty": float(p["qty"]),
                "avg_entry_price": float(p["avg_entry_price"]),
                "current_price": float(p["current_price"]),
                "market_value": float(p["market_value"]),
                "unrealized_pl": float(p["unrealized_pl"]),
                "unrealized_plpc": round(float(p["unrealized_plpc"]) * 100, 2),
            }
            for p in self._check(self.trading.get("/v2/positions"))
        ]

    def clock(self) -> dict:
        c = self._check(self.trading.get("/v2/clock"))
        return {"is_open": c["is_open"], "next_open": c["next_open"], "next_close": c["next_close"]}

    # ---- market data (free IEX feed) ----

    def quotes(self, symbols: list[str]) -> dict:
        snaps = self._check(
            self.data.get("/v2/stocks/snapshots", params={"symbols": ",".join(symbols), "feed": "iex"})
        )
        out = {}
        for sym, s in snaps.items():
            if not s:
                continue
            price = (s.get("latestTrade") or {}).get("p")
            prev = (s.get("prevDailyBar") or {}).get("c")
            out[sym] = {
                "price": price,
                "prev_close": prev,
                "change_pct": round((price - prev) / prev * 100, 2) if price and prev else None,
                "day_volume": (s.get("dailyBar") or {}).get("v"),
            }
        return out

    def daily_bars(self, symbol: str, days: int) -> list[dict]:
        start = (datetime.now(timezone.utc) - timedelta(days=int(days * 1.6) + 5)).date().isoformat()
        data = self._check(
            self.data.get(
                f"/v2/stocks/{symbol}/bars",
                params={"timeframe": "1Day", "start": start, "limit": 1000, "feed": "iex", "adjustment": "all"},
            )
        )
        bars = data.get("bars") or []
        return [{"date": b["t"][:10], "close": b["c"], "volume": b["v"]} for b in bars][-days:]

    def news(self, symbols: list[str], limit: int = 10) -> list[dict]:
        data = self._check(
            self.data.get("/v1beta1/news", params={"symbols": ",".join(symbols), "limit": min(limit, 50)})
        )
        return [
            {"time": n["created_at"], "symbols": n.get("symbols"), "headline": n["headline"], "summary": (n.get("summary") or "")[:400]}
            for n in data.get("news", [])
        ]

    # ---- orders ----

    def submit_market_order(self, symbol: str, side: str, notional: float | None = None, qty: float | None = None) -> dict:
        body = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day"}
        if notional is not None:
            body["notional"] = f"{notional:.2f}"
        else:
            body["qty"] = f"{qty:.9f}".rstrip("0").rstrip(".")
        o = self._check(self.trading.post("/v2/orders", json=body))
        return {"id": o["id"], "status": o["status"]}
