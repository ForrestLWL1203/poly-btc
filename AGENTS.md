# AGENTS.md

## Workspace scope

This repository is a multi-product copy-trade workspace:

- `hyper/` owns all Hyperliquid business logic, CLI entry points, tests, product docs, and deployment tooling.
- `dashboard/` owns the shared Dashboard server/API and React frontend.
- Future product business logic belongs in a separate top-level package such as `polymarket/`.
- `data/` and `secret/` are shared runtime locations, not source modules.

For any Hyperliquid change, including Dashboard projections and controls, read `hyper/AGENTS.md` completely,
then read the private operational notes in `hyper/CLAUDE.md`. `hyper/AGENTS.md` is the authoritative behavioral
contract; `hyper/CLAUDE.md` must not duplicate or override product rules.

## Shared rules

- Keep product business logic out of `dashboard/`; Dashboard code may read product state and use each product's
  explicit command/parameter control plane.
- Do not create root-level product scripts. Add module entry points under the owning product package and invoke
  them with `.venv/bin/python -m ...` from the repository root.
- Always run repository Python commands, tests, compilation, and operational modules with `.venv/bin/python`.
  Never use bare `python3`, `/usr/bin/python3`, or the macOS system Python as an initial attempt. If `.venv` is
  missing or broken, stop and report or repair that environment instead of silently falling back to another
  interpreter; a missing dependency in a non-venv interpreter is not a repository test failure.
- Never expose, print, commit, or copy secrets, private keys, live databases, private target files, or private
  deployment values.
- The active VPS is locally accessible. For every remote check, read the current connection from
  `secret/vps.txt`; never reuse a remembered or hardcoded target, and attempt the canonical configuration
  before reporting that the VPS is inaccessible.
- Use the established local `~/.ssh/id_ed25519` key with `IdentitiesOnly=yes` as the active VPS's primary
  authentication. The password in `secret/vps.txt` is bootstrap/recovery fallback only. Never generate a new
  SSH keypair or add, replace, rotate, or remove a local/remote SSH key unless the user explicitly authorizes
  that exact key operation; routine inspection or deployment is not authorization to change keys.
- Preserve unrelated worktree changes and never use destructive Git resets without explicit approval.
- Update the owning module's docs, tests, launcher/service paths, and build commands when moving an entry point.
- A code deployment is not permission to start, stop, pause, drain, or change the selected Paper/Live mode.
  Preserve the current production runtime state unless the user explicitly asks to change it.

## Current verification

Run from the repository root:

```bash
.venv/bin/python -m compileall -q hyper dashboard
.venv/bin/python -m unittest discover -s hyper/tests
dashboard/web/build.sh
hyper/launcher/web/build.sh
```

Do not substitute another Python interpreter for these commands unless the user explicitly authorizes it.
