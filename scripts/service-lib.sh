#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG_DIR="${LOG_DIR:-$PROJECT_DIR/output/logs}"
RUN_DIR="${RUN_DIR:-$PROJECT_DIR/output/run}"

BACKEND_PORT="${BACKEND_PORT:-9999}"
FRONTEND_PORT="${FRONTEND_PORT:-10086}"
STOP_TIMEOUT_SEC="${STOP_TIMEOUT_SEC:-15}"

BACKEND_PID_FILE="${BACKEND_PID_FILE:-$RUN_DIR/backend.local.pid}"
FRONTEND_PID_FILE="${FRONTEND_PID_FILE:-$RUN_DIR/frontend.local.pid}"
BACKEND_LOG_FILE="${BACKEND_LOG_FILE:-$LOG_DIR/backend.local.log}"
FRONTEND_LOG_FILE="${FRONTEND_LOG_FILE:-$LOG_DIR/frontend.local.log}"

VENV_PYTHON="${VENV_PYTHON:-$PROJECT_DIR/.venv/bin/python}"
VENV_UVICORN="${VENV_UVICORN:-$PROJECT_DIR/.venv/bin/uvicorn}"
NPM_BIN="${NPM_BIN:-npm}"

info() {
  printf '[INFO] %s\n' "$*"
}

warn() {
  printf '[WARN] %s\n' "$*" >&2
}

error() {
  printf '[ERROR] %s\n' "$*" >&2
}

ensure_dirs() {
  mkdir -p "$LOG_DIR" "$RUN_DIR"
}

read_pid() {
  local pid_file="$1"
  if [ -f "$pid_file" ]; then
    tr -d '[:space:]' < "$pid_file"
  fi
}

is_pid_running() {
  local pid="${1:-}"
  [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1
}

validate_target() {
  local target="${1:-all}"
  case "$target" in
    all|backend|frontend)
      ;;
    *)
      error "Invalid target: $target (expected: all|backend|frontend)"
      return 1
      ;;
  esac
}

assert_start_prerequisites() {
  if [ ! -x "$VENV_PYTHON" ]; then
    error "Python runtime not found: $VENV_PYTHON"
    error "Create venv and install deps first."
    return 1
  fi

  if ! command -v "$NPM_BIN" >/dev/null 2>&1; then
    error "npm not found in PATH."
    return 1
  fi

  if [ ! -f "$PROJECT_DIR/frontend/package.json" ]; then
    error "frontend/package.json not found."
    return 1
  fi
}

stop_one_service() {
  local name="$1"
  local pid_file="$2"

  local pid=""
  pid="$(read_pid "$pid_file")"
  if [ -z "$pid" ]; then
    info "$name is not running (no pid file)."
    return 0
  fi

  if ! is_pid_running "$pid"; then
    warn "$name pid file is stale: $pid"
    rm -f "$pid_file"
    return 0
  fi

  info "Stopping $name (pid=$pid)"
  kill "$pid" >/dev/null 2>&1 || true

  local waited=0
  while is_pid_running "$pid" && [ "$waited" -lt "$STOP_TIMEOUT_SEC" ]; do
    sleep 1
    waited=$((waited + 1))
  done

  if is_pid_running "$pid"; then
    warn "$name did not exit in ${STOP_TIMEOUT_SEC}s. Killing forcefully."
    kill -9 "$pid" >/dev/null 2>&1 || true
  fi

  rm -f "$pid_file"
  info "$name stopped."
}

start_backend() {
  ensure_dirs

  local pid=""
  pid="$(read_pid "$BACKEND_PID_FILE")"
  if is_pid_running "$pid"; then
    info "Backend already running (pid=$pid, port=$BACKEND_PORT)."
    return 0
  fi
  rm -f "$BACKEND_PID_FILE"

  local -a backend_cmd
  if [ -x "$VENV_UVICORN" ]; then
    backend_cmd=("$VENV_UVICORN" "backend.server:app" "--host" "0.0.0.0" "--port" "$BACKEND_PORT")
  else
    backend_cmd=("$VENV_PYTHON" "-m" "uvicorn" "backend.server:app" "--host" "0.0.0.0" "--port" "$BACKEND_PORT")
  fi

  (
    cd "$PROJECT_DIR"
    nohup "${backend_cmd[@]}" >"$BACKEND_LOG_FILE" 2>&1 &
    echo "$!" > "$BACKEND_PID_FILE"
  )

  sleep 2
  pid="$(read_pid "$BACKEND_PID_FILE")"
  if ! is_pid_running "$pid"; then
    error "Failed to start backend. Check log: $BACKEND_LOG_FILE"
    tail -n 40 "$BACKEND_LOG_FILE" >&2 || true
    return 1
  fi

  info "Backend started (pid=$pid, url=http://127.0.0.1:$BACKEND_PORT)"
}

start_frontend() {
  ensure_dirs

  local pid=""
  pid="$(read_pid "$FRONTEND_PID_FILE")"
  if is_pid_running "$pid"; then
    info "Frontend already running (pid=$pid, port=$FRONTEND_PORT)."
    return 0
  fi
  rm -f "$FRONTEND_PID_FILE"

  (
    cd "$PROJECT_DIR/frontend"
    nohup "$NPM_BIN" run dev -- --host 0.0.0.0 --port "$FRONTEND_PORT" >"$FRONTEND_LOG_FILE" 2>&1 &
    echo "$!" > "$FRONTEND_PID_FILE"
  )

  sleep 3
  pid="$(read_pid "$FRONTEND_PID_FILE")"
  if ! is_pid_running "$pid"; then
    error "Failed to start frontend. Check log: $FRONTEND_LOG_FILE"
    tail -n 60 "$FRONTEND_LOG_FILE" >&2 || true
    return 1
  fi

  info "Frontend started (pid=$pid, url=http://127.0.0.1:$FRONTEND_PORT)"
}

print_status_line() {
  local name="$1"
  local pid_file="$2"
  local port="$3"
  local pid=""
  pid="$(read_pid "$pid_file")"
  if is_pid_running "$pid"; then
    info "$name: running (pid=$pid, port=$port)"
  else
    info "$name: stopped"
  fi
}

