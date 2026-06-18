#!/usr/bin/env sh
# Agent Deck 本地 daemon launcher。
# 职责：为开发者提供无需 tmux 的 start/stop/status/restart/logs/foreground 入口。
# 副作用：默认在后台启动 `agent-deckd`，占用 127.0.0.1:8765，写 PID 文件和日志文件，
# 并可能接管已连接的 N4 Pro 显示；不会安装系统服务或修改 Codex 配置。

set -eu

APP_NAME="AgentDeck"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
DEFAULT_HOST="127.0.0.1"
DEFAULT_PORT="8765"

if [ "${XDG_STATE_HOME:-}" ]; then
  STATE_DIR="${XDG_STATE_HOME}/agent-deck"
else
  STATE_DIR="${HOME}/Library/Application Support/${APP_NAME}"
fi

if [ "${XDG_CACHE_HOME:-}" ]; then
  LOG_DIR="${XDG_CACHE_HOME}/agent-deck/logs"
else
  LOG_DIR="${HOME}/Library/Logs/${APP_NAME}"
fi

PID_FILE="${AGENT_DECK_PID_FILE:-${STATE_DIR}/agent-deckd.pid}"
LOG_FILE="${AGENT_DECK_LOG_FILE:-${LOG_DIR}/agent-deckd.log}"
ACTION="start"
FOREGROUND=0

usage() {
  cat <<EOF
Usage:
  ./run.sh [start] [agent-deckd args...]
  ./run.sh --foreground [agent-deckd args...]
  ./run.sh foreground [agent-deckd args...]
  ./run.sh stop
  ./run.sh restart [agent-deckd args...]
  ./run.sh status
  ./run.sh logs

Default action is background start. Extra args are passed to agent-deckd.

Files:
  PID: ${PID_FILE}
  Log: ${LOG_FILE}
EOF
}

is_running() {
  if [ ! -f "${PID_FILE}" ]; then
    return 1
  fi
  pid=$(cat "${PID_FILE}" 2>/dev/null || true)
  if [ -z "${pid}" ]; then
    return 1
  fi
  kill -0 "${pid}" 2>/dev/null
}

listener_pid() {
  if ! command -v lsof >/dev/null 2>&1; then
    return 1
  fi
  lsof -tiTCP:"${DEFAULT_PORT}" -sTCP:LISTEN -nP 2>/dev/null | head -n 1
}

daemon_exec() {
  if [ -x "${SCRIPT_DIR}/.venv/bin/agent-deckd" ]; then
    exec "${SCRIPT_DIR}/.venv/bin/agent-deckd" --host "${DEFAULT_HOST}" --port "${DEFAULT_PORT}" "$@"
  fi
  exec uv run agent-deckd --host "${DEFAULT_HOST}" --port "${DEFAULT_PORT}" "$@"
}

start_foreground() {
  mkdir -p "${STATE_DIR}" "${LOG_DIR}"
  cd "${SCRIPT_DIR}"
  echo "Starting agent-deckd in foreground on ${DEFAULT_HOST}:${DEFAULT_PORT}"
  echo "Log file is not used in foreground mode."
  daemon_exec "$@"
}

start_background() {
  mkdir -p "${STATE_DIR}" "${LOG_DIR}"
  if is_running; then
    pid=$(cat "${PID_FILE}")
    echo "agent-deckd already running with PID ${pid}"
    echo "Log: ${LOG_FILE}"
    return 0
  fi
  rm -f "${PID_FILE}"
  launcher_output=$(
    uv run python - "${SCRIPT_DIR}" "${DEFAULT_HOST}" "${DEFAULT_PORT}" "${LOG_FILE}" "$@" <<'PY'
import os
import subprocess
import sys

script_dir, host, port, log_file, *daemon_args = sys.argv[1:]
daemon_bin = os.path.join(script_dir, ".venv", "bin", "agent-deckd")
if os.path.isfile(daemon_bin) and os.access(daemon_bin, os.X_OK):
    command = [daemon_bin, "--host", host, "--port", port, *daemon_args]
else:
    command = ["uv", "run", "agent-deckd", "--host", host, "--port", port, *daemon_args]

log = open(log_file, "ab", buffering=0)
process = subprocess.Popen(
    command,
    cwd=script_dir,
    stdin=subprocess.DEVNULL,
    stdout=log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
    close_fds=True,
)
print(process.pid)
PY
  )
  pid=$(printf '%s\n' "${launcher_output}" | tail -n 1)
  printf '%s\n' "${pid}" >"${PID_FILE}"
  i=0
  while [ "${i}" -lt 60 ]; do
    if ! kill -0 "${pid}" 2>/dev/null; then
      rm -f "${PID_FILE}"
      echo "agent-deckd exited during startup"
      echo "Log: ${LOG_FILE}"
      tail -n 40 "${LOG_FILE}" 2>/dev/null || true
      return 1
    fi
    listen_pid=$(listener_pid || true)
    if [ -n "${listen_pid}" ]; then
      printf '%s\n' "${listen_pid}" >"${PID_FILE}"
      echo "agent-deckd started in background with PID ${listen_pid}"
      echo "Log: ${LOG_FILE}"
      return 0
    fi
    i=$((i + 1))
    sleep 0.5
  done
  echo "agent-deckd started with PID ${pid}, but ${DEFAULT_HOST}:${DEFAULT_PORT} is not listening yet"
  echo "Log: ${LOG_FILE}"
  return 1
}

stop_daemon() {
  if ! is_running; then
    rm -f "${PID_FILE}"
    echo "agent-deckd is not running"
    return 0
  fi
  pid=$(cat "${PID_FILE}")
  echo "Stopping agent-deckd PID ${pid}"
  kill "${pid}" 2>/dev/null || true
  i=0
  while kill -0 "${pid}" 2>/dev/null; do
    i=$((i + 1))
    if [ "${i}" -ge 40 ]; then
      echo "agent-deckd did not stop after 8s; sending SIGKILL"
      kill -9 "${pid}" 2>/dev/null || true
      break
    fi
    sleep 0.2
  done
  rm -f "${PID_FILE}"
  echo "agent-deckd stopped"
}

status_daemon() {
  if is_running; then
    pid=$(cat "${PID_FILE}")
    echo "agent-deckd is running with PID ${pid}"
    echo "PID: ${PID_FILE}"
    echo "Log: ${LOG_FILE}"
  else
    rm -f "${PID_FILE}"
    echo "agent-deckd is not running"
    echo "PID: ${PID_FILE}"
    echo "Log: ${LOG_FILE}"
    return 1
  fi
}

show_logs() {
  mkdir -p "${LOG_DIR}"
  touch "${LOG_FILE}"
  tail -n 80 -f "${LOG_FILE}"
}

if [ "$#" -gt 0 ]; then
  case "$1" in
    start|stop|restart|status|logs|foreground)
      ACTION="$1"
      shift
      ;;
    --foreground)
      ACTION="start"
      FOREGROUND=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
  esac
fi

case "${ACTION}" in
  start)
    if [ "${FOREGROUND}" -eq 1 ]; then
      start_foreground "$@"
    else
      start_background "$@"
    fi
    ;;
  foreground)
    start_foreground "$@"
    ;;
  stop)
    stop_daemon
    ;;
  restart)
    stop_daemon
    start_background "$@"
    ;;
  status)
    status_daemon
    ;;
  logs)
    show_logs
    ;;
  *)
    usage
    exit 2
    ;;
esac
