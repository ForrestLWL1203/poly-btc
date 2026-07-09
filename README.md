# poly-btc

Hyperliquid copy-trade scanner, paper observer, dashboard, and launcher.

The active tree is scoped to Hyperliquid runtime code and ops tooling.

## Active Runtime

| Area | Entry points |
|---|---|
| Scanner/discovery | `hl_discover.py`, `hl/scanner.py`, `hl/metrics.py`, `hl/fills.py` |
| Observer/paper copy | `hl_observe.py`, `hl/observer.py` |
| Dashboard API | `hl_dashboard.py`, `hl/api.py`, `hl/api_*.py` |
| Dashboard frontend | `web/app.jsx`, `web/components/*`, compiled to `web/app.js` |
| Launcher/ops | `launcher/launcher.py`, `launcher/server.py`, `launcher/core/*` |

## Common Commands

For a non-agent operator, start the deployment/ops launcher directly from the repo root:

- macOS: double-click `launcher.command`
- Windows: double-click `launcher.cmd`
- Terminal fallback: `python3 launcher/launcher.py`

The launcher starts only the local ops UI. On first deploy, the dashboard process initializes
`data/hl.db` by running storage migrations and seeding default params. Scanner and observer are then
started from the dashboard or systemd timer, not during launcher boot.

```bash
python3 hl_dashboard.py --db data/hl.db --static web --host 127.0.0.1 --port 8810
python3 hl_discover.py --db data/hl.db scan --days 14 --scan-interval 8
python3 hl_observe.py --db data/hl.db observe
python3 launcher/launcher.py --port 8799 --no-browser
```

## Frontend Build

Dashboard React is precompiled without bundling React itself. Edit JSX, then build:

```bash
web/build.sh
```

Launcher frontend is built separately:

```bash
launcher/web/build.sh
```

## Verification

```bash
python3 -m py_compile hl/*.py hl_*.py launcher/*.py launcher/core/*.py
python3 -m unittest discover tests
```

Secrets and live deployment details belong in local/private files only. Do not commit `secret/`,
`launcher/data/keys/`, `launcher/data/targets.json`, or live DB snapshots.
