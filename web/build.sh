#!/bin/bash
# 预编译 app.jsx → app.js(纯JS,全局React)。改完 app.jsx 部署前先跑本脚本。
cd "$(dirname "$0")"
npx --yes esbuild app.jsx --loader:.jsx=jsx --jsx=transform \
  --jsx-factory=React.createElement --jsx-fragment=React.Fragment \
  --minify --outfile=app.js --log-level=warning && echo "built app.js ($(wc -c < app.js) bytes)"
