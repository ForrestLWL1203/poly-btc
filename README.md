# poly-btc

Read-only collector and analyzer for Polymarket **BTC 5-minute up/down** markets.

It polls the public market trade feed for each 5-minute window, attaches order-book
context (top-of-book reconstructed as of each fill's exchange timestamp), reconstructs
per-wallet / per-window PnL directly from the trade feed, and surfaces recurring
two-sided wallets for further analysis.

No private keys, no orders — observation only.

## Layout

| file | role |
|---|---|
| `collect.py` | single async loop: discover windows, buffer fills with context, settle & aggregate |
| `profile.py` | rank recurring two-sided wallets; per-wallet window breakdown |
| `schema.sql` | SQLite schema (`windows`, `wallet_window`, `trades`) |
| `lib/` | infra: market discovery, Chainlink price feed, CLOB book stream, data API, crypto price |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
# collect (Ctrl-C to stop; runs indefinitely without --seconds)
python collect.py --db btc5min.db

# analyze
python profile.py --db btc5min.db rank --min-windows 5
python profile.py --db btc5min.db wallet <address>
```

PnL is reconstructed only from the unbiased market trade feed and self-computed
settlement (crypto-price open/close); per-wallet positions endpoints are never used.
