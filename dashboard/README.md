# Shared Dashboard

`dashboard/` is the presentation and operator-control layer shared by copy-trade products.

- `server.py` starts the HTTP service.
- `api/` contains endpoint projections and command/parameter control-plane handlers.
- `web/` contains the React frontend source, static assets, mock helpers, and compiled bundle.

The current API projects Hyperliquid state from `hyper/`, but product discovery, selection, execution, and
state mutation remain in the product package. When a Polymarket module is added, product switching and combined
navigation belong here; Polymarket business logic belongs in `polymarket/`.

`/api/params` exposes only the operator-safe basic scanner controls: weekly volume floor, HFT exclusion and
Core capacity. Internal collection, qualification and replay parameters are not part of the Dashboard API.
Scan history is a rolling operational summary: the API and UI expose at most the latest five runs.

The top-bar SQLite space badge is based only on the physical main database file's share of the filesystem:
warning at 20% and critical at 35%. WAL size and short-term active-data growth remain available in the storage
guard diagnostics, but they do not surface as a misleading disk-capacity badge.

Position fill details distinguish margin committed on entry from capital returned on reduce/close. Returned
capital is released entry-basis margin plus realized PnL net of exit fees; it is never exit notional divided by
leverage.
Open-position notional is current exchange-style position value (absolute remaining quantity times mark price),
while closed-position history preserves entry-basis notional.

The Hyperliquid Account tab exposes only Paper and Mainnet Live. Paper is a compact one-line state; selecting
Live reveals encrypted Mainnet Agent setup. Testnet is intentionally absent from product UI. Agent expiry is
read from Hyperliquid during credential verification. Account setup performs only the initial credential checks
and never starts Observer; the top-right transport controls are the only start/pause/stop entry and perform the
full Live startup preflight before creating the session. The play control starts or resumes, pause blocks new
exposure while existing positions remain managed, and stop safely drains Live exposure before process exit.
Mode changes require Observer to be stopped.

Passing that startup preflight enters normal Live execution immediately; there is no separate 1%-equity Canary
stage. Normal equity sizing, deployment, per-coin, add and liquidity limits continue to apply.

The Core-list strict-replay summary is expressed only as 30d/7d ROI. Its denominator is the account equity
frozen at that collection's corresponding replay-window boundary; current Paper or Live balances never rewrite
the published summary, and absolute replay PnL is not presented as an operator KPI.

The wallet control plane exposes `wallet_exit_request` and `wallet_exit_cancel`; Dashboard never mutates selection or execution state
directly. `/api/wallets` projects financial risk, system blocks, operator intent, effective role and entry
permission. A Core-row ban button means conditional exit: flat wallets appear in Challenger immediately;
wallets with open positions in the currently selected Paper/Live ledger show “仅退出中” until Observer resolves
that ledger's captured cohort; positions in the inactive ledger do not block the request and remain exit-only
when that ledger resumes. The sidebar wallet count uses the same effective-Core predicate as the followed tab,
including draining seats and excluding requalify rows. The same control sends
`wallet_exit_cancel` while draining, restoring normal following without closing the existing cohort unless a
durable risk or system block forbids it. Low/medium risk labels are advisory and do not disable entries. High
risk disables entries; cumulative-loss high may later recover
after profitable actual-Copy results, while a confirmed single catastrophic event remains durable. Funds
withdrawal, structural blocks and data anomalies are always rendered as text as well as color.

Run from the repository root:

```bash
DASH_PASSWORD=... python3 -m dashboard.server \
  --db data/hl.db --static dashboard/web --host 127.0.0.1 --port 8810
```

After frontend changes, rebuild with `dashboard/web/build.sh`; never edit `dashboard/web/app.js` by hand.
