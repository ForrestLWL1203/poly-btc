# Copy-trade workspace

This repository is organized as a multi-product copy-trade workspace. Hyperliquid is the active product;
Polymarket can be added later without mixing its business logic into the existing package.

## Directory layout

```text
.
├── hyper/                  Hyperliquid business logic, CLI, tests, docs, and launcher
│   ├── cli/                Scanner, Observer, and offline-lab entry points
│   ├── launcher/           Hyperliquid deployment and operations console
│   ├── tests/              Hyperliquid and current integration tests
│   └── README.md           Product architecture and operating guide
├── dashboard/              Shared Dashboard API and React frontend
│   ├── api/                HTTP endpoint modules
│   ├── server.py           Dashboard service entry point
│   └── web/                Frontend source and compiled bundle
├── data/                   Runtime databases (ignored)
└── secret/                 Runtime credentials (ignored)
```

Future Polymarket business code belongs in a top-level `polymarket/` package. The shared Dashboard can then
provide product switching while importing read/control adapters from both product packages.

## Common commands

Run commands from the repository root:

```bash
python3 -m dashboard.server --db data/hl.db --static dashboard/web --host 127.0.0.1 --port 8810
python3 -m hyper.cli.discover --db data/hl.db scan --days 14 --scan-interval 8
python3 -m hyper.cli.observe --db data/hl.db observe
python3 -m hyper.launcher.launcher --port 8799 --no-browser
```

See [hyper/README.md](hyper/README.md) for the Hyperliquid pipeline, safety invariants, and complete command
reference. Repository-wide contributor rules live in [AGENTS.md](AGENTS.md).
