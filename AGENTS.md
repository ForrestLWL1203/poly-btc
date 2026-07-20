# AGENTS.md

## Workspace scope

This repository is a multi-product copy-trade workspace:

- `hyper/` owns all Hyperliquid business logic, CLI entry points, tests, product docs, and deployment tooling.
- `dashboard/` owns the shared Dashboard server/API and React frontend.
- Future product business logic belongs in a separate top-level package such as `polymarket/`.
- `data/` and `secret/` are shared runtime locations, not source modules.

For any Hyperliquid change, including the current Dashboard's Hyperliquid projections and controls, read
`hyper/AGENTS.md` and then the private local `hyper/CLAUDE.md` before acting.

## Shared rules

- Keep product business logic out of `dashboard/`; Dashboard code may read product state and use each product's
  explicit command/parameter control plane.
- Do not create root-level product scripts. Add module entry points under the owning product package and invoke
  them with `python3 -m ...`.
- Never expose, print, commit, or copy secrets, private keys, live databases, private target files, or private
  deployment values.
- Preserve unrelated worktree changes and never use destructive Git resets without explicit approval.
- Update the owning module's docs, tests, launcher/service paths, and build commands when moving an entry point.

## Current verification

Run from the repository root:

```bash
python3 -m compileall -q hyper dashboard
python3 -m unittest discover -s hyper/tests
dashboard/web/build.sh
hyper/launcher/web/build.sh
```
