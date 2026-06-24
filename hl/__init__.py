"""hl — Hyperliquid copy-trade toolkit.

Layered so the execution leg can be added without churning discovery/observation:
  config, util, rest, ws, fills, metrics, storage   — leaf infra (no business logic)
  scanner                                            — discovery (leaderboard -> watchlist)
  observer, paper                                    — live WS observation + paper-copy sim
Entry points: hl_discover.py (scanner CLI), hl_observe.py (observer CLI).
"""
