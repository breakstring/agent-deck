#!/usr/bin/env bash
# Agent Deck tmux daemon launcher.
# 职责：用 tmux detached session 启动、查看、停止或 attach 本地 agent-deckd。
# 副作用：start/restart 会创建 tmux session，并可能让 agent-deckd 接管 N4 Pro 硬件显示；
# 不安装系统服务、不修改 Codex 配置、不写 git 或项目配置。

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
SESSION_NAME="${AGENT_DECK_TMUX_SESSION:-agent-deckd}"
HOST="${AGENT_DECK_HOST:-127.0.0.1}"
PORT="${AGENT_DECK_PORT:-8765}"
ACTION="${1:-start}"

if [[ $# -gt 0 ]]; then
  shift
fi

usage() {
  cat <<EOF
Usage:
  scripts/agent-deckd-tmux.sh [start] [agent-deckd args...]
  scripts/agent-deckd-tmux.sh restart [agent-deckd args...]
  scripts/agent-deckd-tmux.sh stop
  scripts/agent-deckd-tmux.sh status
  scripts/agent-deckd-tmux.sh attach
  scripts/agent-deckd-tmux.sh logs

Environment:
  AGENT_DECK_TMUX_SESSION   tmux session name, default: ${SESSION_NAME}
  AGENT_DECK_HOST           bind host, default: ${HOST}
  AGENT_DECK_PORT           bind port, default: ${PORT}
EOF
}

require_tmux() {
  if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux is not installed. Install tmux first, then rerun this script." >&2
    exit 127
  fi
}

tmux_session_exists() {
  tmux has-session -t "${SESSION_NAME}" 2>/dev/null
}

listener_pid() {
  if ! command -v lsof >/dev/null 2>&1; then
    return 1
  fi
  lsof -tiTCP:"${PORT}" -sTCP:LISTEN -nP 2>/dev/null | head -n 1
}

daemon_command() {
  local quoted_args=""
  local arg
  for arg in "$@"; do
    printf -v arg "%q" "${arg}"
    quoted_args+=" ${arg}"
  done
  if [[ -x "${PROJECT_ROOT}/.venv/bin/agent-deckd" ]]; then
    printf 'cd %q && exec %q --host %q --port %q%s' \
      "${PROJECT_ROOT}" "${PROJECT_ROOT}/.venv/bin/agent-deckd" "${HOST}" "${PORT}" "${quoted_args}"
  else
    printf 'cd %q && exec uv run agent-deckd --host %q --port %q%s' \
      "${PROJECT_ROOT}" "${HOST}" "${PORT}" "${quoted_args}"
  fi
}

start_daemon() {
  require_tmux
  if tmux_session_exists; then
    echo "tmux session '${SESSION_NAME}' already exists."
    echo "Attach: ${0} attach"
    return 0
  fi
  local pid
  pid="$(listener_pid || true)"
  if [[ -n "${pid}" ]]; then
    echo "agent-deckd already appears to be listening on ${HOST}:${PORT} with PID ${pid}."
    echo "Not starting a second daemon."
    return 0
  fi
  tmux new-session -d -s "${SESSION_NAME}" -c "${PROJECT_ROOT}" "$(daemon_command "$@")"
  echo "Started agent-deckd in tmux session '${SESSION_NAME}' on ${HOST}:${PORT}."
  echo "Attach: ${0} attach"
}

stop_daemon() {
  require_tmux
  if ! tmux_session_exists; then
    echo "tmux session '${SESSION_NAME}' is not running."
    return 0
  fi
  tmux send-keys -t "${SESSION_NAME}" C-c
  sleep 0.5
  if tmux_session_exists; then
    tmux kill-session -t "${SESSION_NAME}"
  fi
  echo "Stopped tmux session '${SESSION_NAME}'."
}

status_daemon() {
  require_tmux
  if tmux_session_exists; then
    echo "tmux session '${SESSION_NAME}' is running."
  else
    echo "tmux session '${SESSION_NAME}' is not running."
  fi
  local pid
  pid="$(listener_pid || true)"
  if [[ -n "${pid}" ]]; then
    echo "Listener: ${HOST}:${PORT} PID ${pid}"
  else
    echo "Listener: ${HOST}:${PORT} not detected"
  fi
}

case "${ACTION}" in
  start)
    start_daemon "$@"
    ;;
  restart)
    stop_daemon
    start_daemon "$@"
    ;;
  stop)
    stop_daemon
    ;;
  status)
    status_daemon
    ;;
  attach)
    require_tmux
    exec tmux attach-session -t "${SESSION_NAME}"
    ;;
  logs)
    require_tmux
    exec tmux capture-pane -t "${SESSION_NAME}" -p -S -200
    ;;
  --help|-h|help)
    usage
    ;;
  *)
    echo "Unknown action: ${ACTION}" >&2
    usage >&2
    exit 2
    ;;
esac
