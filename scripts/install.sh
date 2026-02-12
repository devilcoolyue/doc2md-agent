#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/devilcoolyue/doc2md-agent.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/doc2md-agent}"

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
    fail "This installer only supports Linux, current: ${kernel}"
  fi
}

detect_pkg_manager() {
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
    PKG_MANAGER=""
  fi
}

setup_privilege() {
  if [ "${EUID:-$(id -u)}" -eq 0 ]; then
    SUDO=""
  elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    SUDO=""
  fi
}

install_package() {
  local pkg="$1"
  case "${PKG_MANAGER}" in
    apt)
      $SUDO apt-get update
      $SUDO apt-get install -y "$pkg"
      ;;
    dnf)
      $SUDO dnf install -y "$pkg"
      ;;
    yum)
      $SUDO yum install -y "$pkg"
      ;;
    pacman)
      $SUDO pacman -Sy --noconfirm "$pkg"
      ;;
    zypper)
      $SUDO zypper --non-interactive install "$pkg"
      ;;
    apk)
      $SUDO apk add --no-cache "$pkg"
      ;;
    *)
      fail "git is required, but no supported package manager found (apt/dnf/yum/pacman/zypper/apk)"
      ;;
  esac
}

ensure_git() {
  if command -v git >/dev/null 2>&1; then
    return
  fi

  if [ -z "$SUDO" ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
    fail "git is missing and sudo is unavailable, please install git manually first"
  fi

  log "git not found, installing git..."
  install_package git
  command -v git >/dev/null 2>&1 || fail "git install failed"
}

clone_or_update() {
  if [ -d "${INSTALL_DIR}/.git" ]; then
    log "Repository already exists, pulling latest code..."
    git -C "$INSTALL_DIR" fetch --all --prune
    git -C "$INSTALL_DIR" checkout "$REPO_BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$REPO_BRANCH"
    return
  fi

  if [ -d "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]; then
    fail "INSTALL_DIR is not empty and not a git repository: ${INSTALL_DIR}"
  fi

  log "Cloning repository to ${INSTALL_DIR}..."
  git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
}

run_project_installer() {
  local start_script="${INSTALL_DIR}/deploy/start.sh"

  if [ ! -f "$start_script" ]; then
    fail "start script not found: ${start_script}"
  fi

  chmod +x "$start_script"
  log "Running project installer: ${start_script}"
  exec "$start_script" "$INSTALL_DIR"
}

main() {
  require_linux
  detect_pkg_manager
  setup_privilege
  ensure_git
  clone_or_update
  run_project_installer
}

main "$@"
