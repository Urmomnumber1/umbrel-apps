"""SQLite storage: settings, agent runs, run events, and trade proposals."""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("STOCKSAI_DB", "/data/stocksai.db")

DEFAULT_SETTINGS = {
    # Credentials (entered in the UI; env vars are used as a fallback)
    "anthropic_api_key": "",
    "alpaca_key_id": "",
    "alpaca_secret": "",
    "alpaca_paper": True,
    # AI
    "model": "claude-opus-5-5",
    "effort": "high",
    "strategy": (
        "Long-term, diversified growth. Prefer broad ETFs and large, profitable companies. "
        "Avoid chasing short-term moves. Rebalance gradually; doing nothing is fine."
    ),
    # Autonomy: "approve" = every trade waits for a human; "auto" = executes within risk limits
    "mode": "approve",
    "trading_enabled": False,  # kill switch: nothing executes while False
    "schedule_enabled": False,
    "interval_minutes": 240,
    # Universe
    "watchlist": ["SPY", "QQQ", "VTI", "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"],
    "restrict_to_watchlist": True,
    # Hard risk limits (enforced in risk.py, not by the AI)
    "max_order_usd": 500.0,
    "max_position_pct": 20.0,
    "min_cash_pct": 10.0,
    "max_trades_per_day": 5,
    "max_daily_loss_pct": 3.0,
}

SECRET_KEYS = {"anthropic_api_key", "alpaca_key_id", "alpaca_secret"}
ENV_FALLBACK = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "alpaca_key_id": "ALPACA_KEY_ID",
    "alpaca_secret": "ALPACA_SECRET",
}

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL, finished_at TEXT,
                trigger TEXT NOT NULL, status TEXT NOT NULL, summary TEXT,
                input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER, created_at TEXT NOT NULL,
                symbol TEXT NOT NULL, side TEXT NOT NULL,
                notional_usd REAL, qty REAL, reason TEXT,
                status TEXT NOT NULL, risk_notes TEXT,
                order_id TEXT, decided_at TEXT
            );
            """
        )
    return _conn


def execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        cur = conn().execute(sql, params)
        conn().commit()
        return cur.lastrowid


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, params).fetchall()]


# ---- settings ----

def get_settings() -> dict:
    stored = {r["key"]: json.loads(r["value"]) for r in query("SELECT key, value FROM settings")}
    s = {**DEFAULT_SETTINGS, **stored}
    for key, env in ENV_FALLBACK.items():
        if not s[key]:
            s[key] = os.environ.get(env, "")
    return s


def update_settings(changes: dict) -> None:
    for key, value in changes.items():
        if key in DEFAULT_SETTINGS:
            execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )


def public_settings() -> dict:
    """Settings safe to send to the browser: secrets reduced to a set/unset flag."""
    s = get_settings()
    for key in SECRET_KEYS:
        s[key] = bool(s[key])
    return s


# ---- runs / events ----

def start_run(trigger: str) -> int:
    return execute("INSERT INTO runs (started_at, trigger, status) VALUES (?, ?, 'running')", (now_iso(), trigger))


def finish_run(run_id: int, status: str, summary: str, input_tokens: int, output_tokens: int) -> None:
    execute(
        "UPDATE runs SET finished_at=?, status=?, summary=?, input_tokens=?, output_tokens=? WHERE id=?",
        (now_iso(), status, summary, input_tokens, output_tokens, run_id),
    )


def log_event(run_id: int, kind: str, data) -> None:
    execute("INSERT INTO events (run_id, ts, kind, data) VALUES (?, ?, ?, ?)", (run_id, now_iso(), kind, json.dumps(data)))


# ---- proposals ----

def add_proposal(run_id, symbol, side, notional_usd, qty, reason, status, risk_notes="") -> int:
    return execute(
        "INSERT INTO proposals (run_id, created_at, symbol, side, notional_usd, qty, reason, status, risk_notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, now_iso(), symbol, side, notional_usd, qty, reason, status, risk_notes),
    )


def set_proposal(pid: int, status: str, risk_notes: str | None = None, order_id: str | None = None) -> None:
    execute(
        "UPDATE proposals SET status=?, risk_notes=COALESCE(?, risk_notes), order_id=COALESCE(?, order_id), decided_at=? WHERE id=?",
        (status, risk_notes, order_id, now_iso(), pid),
    )


def get_proposal(pid: int) -> dict | None:
    rows = query("SELECT * FROM proposals WHERE id=?", (pid,))
    return rows[0] if rows else None


def executed_today_count(day_prefix: str) -> int:
    rows = query("SELECT COUNT(*) AS n FROM proposals WHERE status='executed' AND decided_at LIKE ?", (day_prefix + "%",))
    return rows[0]["n"]
