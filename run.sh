#!/usr/bin/env sh
# 启动本机 Agent Deck daemon；默认启用 N4 Pro 统一渲染链路。
# 副作用：占用本机 127.0.0.1:8765，并可能接管已连接的 N4 Pro 显示。

set -eu

cd "$(dirname "$0")"
exec uv run agent-deckd --host 127.0.0.1 --port 8765 "$@"
