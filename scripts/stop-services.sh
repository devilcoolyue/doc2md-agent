#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./service-lib.sh
source "$SCRIPT_DIR/service-lib.sh"

TARGET="${1:-all}"
validate_target "$TARGET"

case "$TARGET" in
  all)
    stop_one_service "frontend" "$FRONTEND_PID_FILE"
    stop_one_service "backend" "$BACKEND_PID_FILE"
    ;;
  backend)
    stop_one_service "backend" "$BACKEND_PID_FILE"
    ;;
  frontend)
    stop_one_service "frontend" "$FRONTEND_PID_FILE"
    ;;
esac

print_status_line "backend" "$BACKEND_PID_FILE" "$BACKEND_PORT"
print_status_line "frontend" "$FRONTEND_PID_FILE" "$FRONTEND_PORT"
