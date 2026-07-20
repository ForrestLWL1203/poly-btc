# Shared Dashboard

`dashboard/` is the presentation and operator-control layer shared by copy-trade products.

- `server.py` starts the HTTP service.
- `api/` contains endpoint projections and command/parameter control-plane handlers.
- `web/` contains the React frontend source, static assets, mock helpers, and compiled bundle.

The current API projects Hyperliquid state from `hyper/`, but product discovery, selection, execution, and
state mutation remain in the product package. When a Polymarket module is added, product switching and combined
navigation belong here; Polymarket business logic belongs in `polymarket/`.

Run from the repository root:

```bash
DASH_PASSWORD=... python3 -m dashboard.server \
  --db data/hl.db --static dashboard/web --host 127.0.0.1 --port 8810
```

After frontend changes, rebuild with `dashboard/web/build.sh`; never edit `dashboard/web/app.js` by hand.
