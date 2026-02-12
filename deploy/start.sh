#!/usr/bin/env bash
set -euo pipefail

BACKEND_PORT="${BACKEND_PORT:-9999}"
FRONTEND_PORT="${FRONTEND_PORT:-10086}"
DEFAULT_PROVIDER="${DEFAULT_PROVIDER:-deepseek}"
DEFAULT_DEEPSEEK_MODEL="${DEFAULT_DEEPSEEK_MODEL:-deepseek-chat}"
DEFAULT_DEEPSEEK_BASE_URL="https://api.deepseek.com/v1"

PROJECT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_DIR="$PROJECT_DIR/.venv"
LOG_DIR="$PROJECT_DIR/output/logs"
RUN_DIR="$PROJECT_DIR/output/run"
BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
BACKEND_LOG_FILE="$LOG_DIR/backend.log"
FRONTEND_LOG_FILE="$LOG_DIR/frontend.log"

PKG_MANAGER=""
PKG_CACHE_UPDATED=0
PYTHON_CMD="python3"

log() {
  echo "[INFO] $*"
}

warn() {
  echo "[WARN] $*" >&2
}

fail() {
  echo "[ERROR] $*" >&2
  exit 1
}

require_linux() {
  local kernel
  kernel="$(uname -s)"
  if [ "$kernel" != "Linux" ]; then
    fail "当前系统是 ${kernel}，该脚本仅支持 Linux"
  fi
}

setup_privilege() {
  if [ "${EUID:-$(id -u)}" -eq 0 ]; then
    SUDO=""
    return
  fi

  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    fail "需要 root 或 sudo 权限来安装系统依赖"
  fi
}

detect_linux_distribution() {
  local os_id="unknown"
  local os_version="unknown"
  local os_like="unknown"

  if [ -f /etc/os-release ]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    os_id="${ID:-unknown}"
    os_version="${VERSION_ID:-unknown}"
    os_like="${ID_LIKE:-unknown}"
  fi

  log "检测到 Linux 发行版: ${os_id} ${os_version} (like: ${os_like})"

  if command -v apt-get >/dev/null 2>&1; then
    PKG_MANAGER="apt"
  elif command -v dnf >/dev/null 2>&1; then
    PKG_MANAGER="dnf"
  elif command -v yum >/dev/null 2>&1; then
    PKG_MANAGER="yum"
  elif command -v pacman >/dev/null 2>&1; then
    PKG_MANAGER="pacman"
  elif command -v zypper >/dev/null 2>&1; then
    PKG_MANAGER="zypper"
  elif command -v apk >/dev/null 2>&1; then
    PKG_MANAGER="apk"
  else
    fail "未识别到支持的包管理器（apt/dnf/yum/pacman/zypper/apk）"
  fi
}

update_package_cache_if_needed() {
  if [ "$PKG_CACHE_UPDATED" -eq 1 ]; then
    return
  fi

  case "$PKG_MANAGER" in
    apt)
      $SUDO apt-get update
      ;;
    pacman)
      $SUDO pacman -Sy --noconfirm
      ;;
    *)
      ;;
  esac

  PKG_CACHE_UPDATED=1
}

install_packages() {
  update_package_cache_if_needed

  case "$PKG_MANAGER" in
    apt)
      $SUDO apt-get install -y "$@"
      ;;
    dnf)
      $SUDO dnf install -y "$@"
      ;;
    yum)
      $SUDO yum install -y "$@"
      ;;
    pacman)
      $SUDO pacman -S --noconfirm "$@"
      ;;
    zypper)
      $SUDO zypper --non-interactive install "$@"
      ;;
    apk)
      $SUDO apk add --no-cache "$@"
      ;;
    *)
      fail "不支持的包管理器: $PKG_MANAGER"
      ;;
  esac
}

ensure_tool() {
  local tool_name="$1"
  shift

  if command -v "$tool_name" >/dev/null 2>&1; then
    return
  fi

  log "检测到缺少 ${tool_name}，开始安装..."
  install_packages "$@"
}

install_pandoc_if_needed() {
  if command -v pandoc >/dev/null 2>&1; then
    log "pandoc 已安装: $(pandoc --version | head -n 1)"
    return
  fi

  log "检测到缺少 pandoc，开始安装..."
  install_packages pandoc
  command -v pandoc >/dev/null 2>&1 || fail "pandoc 安装失败，请手动安装后重试"
}

ensure_python_runtime() {
  case "$PKG_MANAGER" in
    apt)
      ensure_tool python3 python3
      ensure_tool pip3 python3-pip
      if ! python3 -m venv --help >/dev/null 2>&1; then
        install_packages python3-venv
      fi
      ;;
    dnf|yum)
      ensure_tool python3 python3
      ensure_tool pip3 python3-pip
      if ! python3 -m venv --help >/dev/null 2>&1; then
        install_packages python3-virtualenv
      fi
      ;;
    pacman)
      ensure_tool python3 python python-pip
      ;;
    zypper)
      ensure_tool python3 python3 python3-pip
      if ! python3 -m venv --help >/dev/null 2>&1; then
        install_packages python3-virtualenv
      fi
      ;;
    apk)
      ensure_tool python3 python3 py3-pip
      if ! python3 -m venv --help >/dev/null 2>&1; then
        install_packages py3-virtualenv
      fi
      ;;
    *)
      fail "未知包管理器，无法安装 Python 运行环境"
      ;;
  esac

  if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
  else
    fail "未找到可用的 Python 命令"
  fi

  local py_version
  py_version="$("$PYTHON_CMD" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
  log "Python 版本: ${py_version}"
}

ensure_node_runtime() {
  if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
    log "Node.js 已安装: $(node -v), npm: $(npm -v)"
    return
  fi

  log "检测到缺少 Node.js/npm，开始安装..."
  case "$PKG_MANAGER" in
    apt|dnf|yum|pacman|zypper|apk)
      install_packages nodejs npm
      ;;
    *)
      fail "未知包管理器，无法安装 Node.js"
      ;;
  esac
}

git_pull_latest() {
  cd "$PROJECT_DIR"

  if ! command -v git >/dev/null 2>&1; then
    ensure_tool git git
  fi

  if [ ! -d "$PROJECT_DIR/.git" ]; then
    warn "当前目录不是 Git 仓库，跳过拉取代码"
    return
  fi

  if ! git remote get-url origin >/dev/null 2>&1; then
    warn "未找到 origin 远程仓库，跳过拉取代码"
    return
  fi

  if ! git diff --quiet || ! git diff --cached --quiet; then
    warn "检测到未提交变更，跳过 git pull（避免覆盖本地修改）"
    return
  fi

  log "拉取最新代码..."
  git fetch --all --prune
  if git rev-parse --abbrev-ref --symbolic-full-name "@{u}" >/dev/null 2>&1; then
    git pull --ff-only
  else
    warn "当前分支未设置上游分支，跳过 git pull"
  fi
}

escape_yaml_value() {
  local raw="$1"
  raw="${raw//\\/\\\\}"
  raw="${raw//\"/\\\"}"
  printf '%s' "$raw"
}

write_default_llm_config() {
  cd "$PROJECT_DIR"

  local overwrite="Y"
  if [ -f config.yaml ]; then
    read -r -p "检测到 config.yaml，是否覆盖为默认 ${DEFAULT_PROVIDER} 配置? [Y/n]: " overwrite
    overwrite="${overwrite:-Y}"
  fi

  case "$overwrite" in
    y|Y|yes|YES|"")
      ;;
    *)
      log "保留现有 config.yaml"
      return
      ;;
  esac

  local api_key=""
  while [ -z "$api_key" ]; do
    read -r -p "请输入 DeepSeek API Key（直接粘贴即可）: " api_key
    if [ -z "$api_key" ]; then
      warn "API Key 不能为空，请重新输入"
    fi
  done

  local escaped_api_key
  escaped_api_key="$(escape_yaml_value "$api_key")"

  cat > config.yaml <<EOF
# 由 deploy/start.sh 自动生成
provider: "${DEFAULT_PROVIDER}"

providers:
  deepseek:
    api_key: "${escaped_api_key}"
    base_url: "${DEFAULT_DEEPSEEK_BASE_URL}"
    model: "${DEFAULT_DEEPSEEK_MODEL}"
    max_tokens: 16000

conversion:
  chunk_size: 8000
  language: "auto"
  generate_toc: true
  highlight_json: true
  image_dir: "images"
EOF

  log "已写入默认模型配置: config.yaml"
}

install_python_dependencies() {
  cd "$PROJECT_DIR"
  log "安装 Python 依赖..."
  "$PYTHON_CMD" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/pip" install -r requirements.txt
}

install_frontend_dependencies() {
  if [ ! -f "$PROJECT_DIR/frontend/package.json" ]; then
    warn "未找到 frontend/package.json，跳过前端依赖安装"
    return
  fi

  log "安装前端依赖..."
  cd "$PROJECT_DIR/frontend"
  npm install
}

stop_if_running() {
  local name="$1"
  local pid_file="$2"

  if [ ! -f "$pid_file" ]; then
    return
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [ -z "${pid}" ]; then
    rm -f "$pid_file"
    return
  fi

  if kill -0 "$pid" >/dev/null 2>&1; then
    log "停止已有 ${name} 进程 (PID: ${pid})"
    kill "$pid"
    sleep 1
    if kill -0 "$pid" >/dev/null 2>&1; then
      warn "${name} 进程未退出，执行强制停止"
      kill -9 "$pid"
    fi
  fi

  rm -f "$pid_file"
}

start_backend() {
  cd "$PROJECT_DIR"
  log "启动后端服务 (0.0.0.0:${BACKEND_PORT})..."
  nohup "$VENV_DIR/bin/uvicorn" backend.server:app --host 0.0.0.0 --port "$BACKEND_PORT" >"$BACKEND_LOG_FILE" 2>&1 &
  echo "$!" > "$BACKEND_PID_FILE"
}

start_frontend() {
  cd "$PROJECT_DIR/frontend"
  log "启动前端服务 (0.0.0.0:${FRONTEND_PORT})..."
  nohup npm run dev -- --host 0.0.0.0 --port "$FRONTEND_PORT" >"$FRONTEND_LOG_FILE" 2>&1 &
  echo "$!" > "$FRONTEND_PID_FILE"
}

verify_process_started() {
  local name="$1"
  local pid_file="$2"

  if [ ! -f "$pid_file" ]; then
    fail "${name} 未生成 PID 文件，启动失败"
  fi

  local pid
  pid="$(cat "$pid_file")"
  sleep 2
  if ! kill -0 "$pid" >/dev/null 2>&1; then
    fail "${name} 启动失败，请检查日志"
  fi
}

main() {
  require_linux
  setup_privilege
  detect_linux_distribution
  git_pull_latest
  install_pandoc_if_needed
  ensure_python_runtime
  ensure_node_runtime
  write_default_llm_config
  install_python_dependencies
  install_frontend_dependencies

  mkdir -p "$LOG_DIR" "$RUN_DIR"
  stop_if_running "后端" "$BACKEND_PID_FILE"
  stop_if_running "前端" "$FRONTEND_PID_FILE"

  start_backend
  start_frontend
  verify_process_started "后端" "$BACKEND_PID_FILE"
  verify_process_started "前端" "$FRONTEND_PID_FILE"

  log "启动完成"
  log "前端访问地址: http://<服务器IP>:${FRONTEND_PORT}"
  log "后端访问地址: http://<服务器IP>:${BACKEND_PORT}"
  log "后端日志: ${BACKEND_LOG_FILE}"
  log "前端日志: ${FRONTEND_LOG_FILE}"
}

main "$@"
