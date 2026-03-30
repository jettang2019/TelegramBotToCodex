#!/bin/zsh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

CONFIG_FILE="${CONFIG_FILE:-config.toml}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"
STATE_DIR="${STATE_DIR:-.local}"
PID_FILE="${PID_FILE:-$STATE_DIR/service.pid}"
LOG_FILE="${LOG_FILE:-$STATE_DIR/service.log}"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

PYTHONPATH_VALUE="${PYTHONPATH:-}"
if [[ -n "$PYTHONPATH_VALUE" ]]; then
  export PYTHONPATH="$ROOT_DIR/src:$PYTHONPATH_VALUE"
else
  export PYTHONPATH="$ROOT_DIR/src"
fi

mkdir -p "$STATE_DIR"

command="${1:-}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/service.sh run
  ./scripts/service.sh start
  ./scripts/service.sh stop
  ./scripts/service.sh restart
  ./scripts/service.sh status
  ./scripts/service.sh logs

Optional environment overrides:
  CONFIG_FILE=config.toml
  LOG_LEVEL=INFO
  PID_FILE=.local/service.pid
  LOG_FILE=.local/service.log
  PYTHON_BIN=.venv/bin/python
EOF
}

find_matching_pids() {
  pgrep -f "telegram_bot_to_codex --config $CONFIG_FILE" 2>/dev/null || true
}

find_unmanaged_pids() {
  local managed_pid=""
  if [[ -f "$PID_FILE" ]]; then
    managed_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  fi

  local pid
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    if [[ -n "$managed_pid" && "$pid" == "$managed_pid" ]]; then
      continue
    fi
    echo "$pid"
  done < <(find_matching_pids)
}

is_running() {
  if [[ ! -f "$PID_FILE" ]]; then
    return 1
  fi

  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    return 1
  fi

  if kill -0 "$pid" 2>/dev/null; then
    return 0
  fi

  rm -f "$PID_FILE"
  return 1
}

run_service() {
  exec "$PYTHON_BIN" -u -m telegram_bot_to_codex --config "$CONFIG_FILE" --log-level "$LOG_LEVEL"
}

start_service() {
  local unmanaged_pids
  unmanaged_pids="$(find_unmanaged_pids)"
  if [[ -n "$unmanaged_pids" ]]; then
    echo "Service appears to already be running without a PID file."
    echo "PID(s): ${unmanaged_pids//$'\n'/, }"
    echo "Stop that process first or run ./scripts/service.sh stop if you want this script to stop it."
    return 0
  fi

  if is_running; then
    echo "Service is already running with PID $(cat "$PID_FILE")."
    return 0
  fi

  touch "$LOG_FILE"
  nohup "$PYTHON_BIN" -u -m telegram_bot_to_codex --config "$CONFIG_FILE" --log-level "$LOG_LEVEL" >>"$LOG_FILE" 2>&1 &!
  local pid=$!
  echo "$pid" > "$PID_FILE"
  sleep 1

  if kill -0 "$pid" 2>/dev/null; then
    echo "Service started."
    echo "PID: $pid"
    echo "Log: $LOG_FILE"
    return 0
  fi

  echo "Service failed to start. Check $LOG_FILE"
  rm -f "$PID_FILE"
  return 1
}

stop_service() {
  local pid=""
  if is_running; then
    pid="$(cat "$PID_FILE")"
  else
    local unmanaged_pids
    unmanaged_pids="$(find_unmanaged_pids)"
    if [[ -z "$unmanaged_pids" ]]; then
      echo "Service is not running."
      return 0
    fi

    echo "Stopping unmanaged service PID(s): ${unmanaged_pids//$'\n'/, }"
    while IFS= read -r unmanaged_pid; do
      [[ -z "$unmanaged_pid" ]] && continue
      kill "$unmanaged_pid" 2>/dev/null || true
    done <<< "$unmanaged_pids"
    rm -f "$PID_FILE"
    echo "Service stopped."
    return 0
  fi

  kill "$pid" 2>/dev/null || true

  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [[ "$waited" -ge 50 ]]; then
      echo "Service did not stop gracefully. Sending SIGKILL to PID $pid."
      kill -9 "$pid" 2>/dev/null || true
      break
    fi
    sleep 0.1
    waited=$((waited + 1))
  done

  rm -f "$PID_FILE"
  echo "Service stopped."
  return 0
}

status_service() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "Service is running."
    echo "PID: $pid"
    echo "Config: $CONFIG_FILE"
    echo "Log: $LOG_FILE"
    return 0
  fi

  local unmanaged_pids
  unmanaged_pids="$(find_unmanaged_pids)"
  if [[ -n "$unmanaged_pids" ]]; then
    echo "Service is running without a PID file."
    echo "PID(s): ${unmanaged_pids//$'\n'/, }"
    echo "Config: $CONFIG_FILE"
    echo "Log: $LOG_FILE"
    return 0
  fi

  echo "Service is not running."
  echo "Config: $CONFIG_FILE"
  echo "Log: $LOG_FILE"
  return 1
}

logs_service() {
  touch "$LOG_FILE"
  exec tail -f "$LOG_FILE"
}

case "$command" in
  run)
    run_service
    ;;
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    stop_service
    start_service
    ;;
  status)
    status_service
    ;;
  logs)
    logs_service
    ;;
  *)
    usage
    exit 1
    ;;
esac
