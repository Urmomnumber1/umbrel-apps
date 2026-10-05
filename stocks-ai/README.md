# Stocks AI

A self-hosted Umbrel app. Claude reviews your Alpaca brokerage account, prices and news, then proposes trades.
Plain code checks every proposal against hard risk limits, and by default each trade waits for your approval.

## How it works

```
Scheduler / "Run AI now"
        │
        ▼
Claude (agent.py) ── read-only tools: get_account, get_positions, get_quotes,
        │                                get_price_history, get_news
        │
        └─ propose_trade ─► risk.py (hard limits, plain code)
                                 │ pass                  │ fail
                                 ▼                       ▼
                    mode=approve: queued for you    rejected, reason sent back to Claude
                    mode=auto:    executed
                                 │
                                 ▼
         trader.py: kill switch + risk re-check with fresh data ─► Alpaca market order
```

Safety rails, all enforced in code:

- **Paper account by default.** Live trading is locked unless the container has `ALLOW_LIVE_TRADING=1`, and even then you have to switch it on in Settings.
- **Master switch.** Nothing executes while "Trading on" is unchecked. **Stop all trading** turns off trading and the schedule.
- **Approve mode by default.** Proposals wait for you, and they expire at the end of the day.
- **Risk limits:** max order size, max position %, cash reserve, max trades per day, and buys pause after a daily loss limit. There is no shorting, no options, no margin, and orders are market orders only. Trades can be restricted to the watchlist.
- Limits are checked twice: when the AI proposes a trade, and again with fresh data at execution time.
- Every run is logged with Claude's tool calls, results and reasoning. Each AI run is capped at 20 turns and 5 proposals.

## Setup

1. **Alpaca:** create a free account at alpaca.markets and generate **paper trading** API keys.
2. **Anthropic:** create an API key at platform.claude.com.
3. Install the app (below), open it from the Umbrel dashboard, and paste the keys into **Settings**.
   Keys are stored only in the app's data folder on your Umbrel.

## Installing on Umbrel

Umbrel apps run from a published Docker image, so first build and push the image:

```bash
cd app
docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/YOUR_GITHUB_USER/stocks-ai:0.1.0 --push .
```

Then (already done: the app is listed in https://github.com/Urmomnumber1/hermes-umbrel-store):

1. Put `umbrel-store/` in its own GitHub repo, which becomes your community app store.
2. In `umbrel-store/anjalo-stocks-ai/docker-compose.yml`, set `image:` to the image you pushed.
3. On Umbrel, go to **App Store → ⋯ → Community App Stores**, add the repo URL, and install **Stocks AI**.

Umbrel's login protects the app, through its app proxy.

## Running locally

```bash
pip install -r app/requirements.txt
STOCKSAI_DB=./dev-data/stocksai.db uvicorn --app-dir app stocksai.main:app --port 8417
```

Tests run fully offline, using a fake broker and a scripted fake Claude:

```bash
pip install pytest && pytest tests
```

## Cost

Each run uses Claude Opus 5.5 by default and usually costs somewhere around $0.10–$0.50, depending on how much research it does.
Token counts are shown on every run. To cut cost, lower the effort level, switch to Sonnet 5.5, or run less often.
The default schedule is every 4 hours during market hours, which works out to about 2 runs per trading day.

## Caveats

- The free Alpaca data feed (IEX) only covers part of market volume, so prices and volume can differ a little from consolidated quotes.
- There's no good evidence that LLMs beat the market. Run it on paper for a while and compare the results to simply holding an index fund before you consider real money.
