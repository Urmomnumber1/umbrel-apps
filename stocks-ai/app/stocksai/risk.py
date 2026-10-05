"""Hard risk rules. These run in plain code on every proposal, before and at execution.
The AI never decides whether a trade passes these checks."""
import re

SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def check(p: dict, s: dict, account: dict, positions: list[dict], clock: dict, trades_today: int) -> list[str]:
    """Return a list of violations; empty means the trade is allowed."""
    problems = []
    symbol, side = p.get("symbol", ""), p.get("side")
    notional, qty = p.get("notional_usd"), p.get("qty")
    equity = account["equity"]
    held = next((x for x in positions if x["symbol"] == symbol), None)

    if not SYMBOL_RE.match(symbol):
        problems.append(f"Invalid symbol '{symbol}'.")
    if s["restrict_to_watchlist"] and symbol not in s["watchlist"] and not held:
        problems.append(f"{symbol} is not on the watchlist.")
    if not clock["is_open"]:
        problems.append("Market is closed.")
    if account.get("trading_blocked"):
        problems.append("Broker account is blocked from trading.")
    if trades_today >= s["max_trades_per_day"]:
        problems.append(f"Daily trade limit reached ({s['max_trades_per_day']}).")

    if side == "buy":
        if not isinstance(notional, (int, float)) or notional <= 0:
            problems.append("Buys need a positive notional_usd.")
            return problems
        if qty is not None:
            problems.append("Buys use notional_usd only, not qty.")
        if notional > s["max_order_usd"]:
            problems.append(f"Order ${notional:,.2f} exceeds max order size ${s['max_order_usd']:,.2f}.")
        cash_floor = equity * s["min_cash_pct"] / 100
        if account["cash"] - notional < cash_floor:
            problems.append(
                f"Would leave ${account['cash'] - notional:,.2f} cash, below the {s['min_cash_pct']}% reserve (${cash_floor:,.2f})."
            )
        new_value = (held["market_value"] if held else 0) + notional
        if equity > 0 and new_value / equity * 100 > s["max_position_pct"]:
            problems.append(
                f"{symbol} would be {new_value / equity * 100:.1f}% of the portfolio (max {s['max_position_pct']}%)."
            )
        if account["last_equity"] > 0:
            day_pl_pct = (equity - account["last_equity"]) / account["last_equity"] * 100
            if day_pl_pct <= -s["max_daily_loss_pct"]:
                problems.append(f"Portfolio is down {day_pl_pct:.2f}% today; buys paused (limit -{s['max_daily_loss_pct']}%).")
    elif side == "sell":
        if not isinstance(qty, (int, float)) or qty <= 0:
            problems.append("Sells need a positive qty (shares).")
            return problems
        if notional is not None:
            problems.append("Sells use qty only, not notional_usd.")
        if not held:
            problems.append(f"No {symbol} position to sell (short selling is not allowed).")
        elif qty > held["qty"] + 1e-9:
            problems.append(f"Cannot sell {qty} {symbol}; only {held['qty']} held (no shorting).")
    else:
        problems.append("Side must be 'buy' or 'sell'.")

    return problems
