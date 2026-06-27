# Dashboard frontend (preview-grade)

Build-free React (React UMD + `babel-standalone` compiling `app.jsx` in the browser — the poly-fight
pattern). This is a **preview** to validate the UI against the real API; it will be ported to
React + Vite + TS when productionized.

```
web/
├── index.html      entry (loads vendor + app.jsx via babel)
├── app.css         glassmorphism design system (tokens from doc/design_handoff README §7)
├── app.jsx         the whole app: login + 5 pages (Overview/Positions/Wallets/Discovery/Settings)
├── vendor/         React/ReactDOM/Babel UMD bundles  (GITIGNORED — see below)
└── dev/            preview-only helpers (NOT used in production)
    ├── seed_mock.py     seed a realistic mock data/hl_mock.db
    └── mock_consumer.py runs the REAL observer command loop + a scanner/rolling simulator
```

## Run the preview

1. **Vendor libs** (gitignored, ~3MB). Copy once from poly-fight, or fetch:
   ```
   mkdir -p web/vendor && cp /path/to/poly-fight/poly_fight/dashboardV2/vendor/{react-18.3.1.production.min.js,react-dom-18.3.1.production.min.js,babel-standalone-7.29.0.min.js} web/vendor/
   ```
2. **Seed mock data:** `python3 web/dev/seed_mock.py data/hl_mock.db`
3. **Run the mock consumer** (so UI actions + rolling/rescan status are live):
   `python3 web/dev/mock_consumer.py data/hl_mock.db`
4. **Serve** (static + API): `DASH_PASSWORD=mock123 python3 hl_dashboard.py --db data/hl_mock.db --static web --port 8810`
   (or use `.claude/launch.json` → name `dashboard`). Open http://127.0.0.1:8810 (auto-logs in with `mock123`).

## Against the REAL system
Point `--db` at the live `data/hl.db` (fed by the real observer/scanner) and drop the mock consumer.
The frontend already speaks the real API contract — no frontend change needed.
See `doc/dashboard-landing-plan.md` for the remaining engine-side wiring (params-from-DB, scanner
rolling/rescan writes) and the planned SSE upgrade for refresh.
```
