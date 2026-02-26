#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./service-lib.sh
source "$SCRIPT_DIR/service-lib.sh"

TARGET="${1:-all}"
validate_target "$TARGET"

assert_start_prerequisites

case "$TARGET" in
  all)
    start_backend
    start_frontend
    ;;
  backend)
    start_backend
    ;;
  frontend)
    start_frontend
    ;;
esac

print_status_line "backend" "$BACKEND_PID_FILE" "$BACKEND_PORT"
print_status_line "frontend" "$FRONTEND_PID_FILE" "$FRONTEND_PORT"
info "Logs: $BACKEND_LOG_FILE | $FRONTEND_LOG_FILE"
