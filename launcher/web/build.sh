#!/usr/bin/env bash
# Compile app.jsx -> app.js (esbuild). Run after editing app.jsx, before using the launcher.
# References global React/ReactDOM (loaded via vendor/), same as the dashboard — does NOT bundle React.
set -e
cd "$(dirname "$0")"
npx --yes esbuild app.jsx \
  --loader:.jsx=jsx --jsx=transform \
  --jsx-factory=React.createElement --jsx-fragment=React.Fragment \
  --minify --outfile=app.js
echo "built app.js ($(wc -c < app.js) bytes)"
