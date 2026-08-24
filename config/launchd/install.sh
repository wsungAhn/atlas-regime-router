#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$SCRIPT_DIR/com.atlas.options-runner.plist"
TARGET="$HOME/Library/LaunchAgents/com.atlas.options-runner.plist"

mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST" "$TARGET"

if launchctl list | grep -q com.atlas.options-runner; then
  echo "[INFO] 기존 잡 언로드 후 재등록"
  launchctl bootout "gui/$(id -u)" "$TARGET" 2>/dev/null || true
fi

launchctl bootstrap "gui/$(id -u)" "$TARGET"
echo "[OK] com.atlas.options-runner 등록 완료 — 9:30~16:00 ET 15분 간격, 장 열려있을 때만 실제 동작"
echo "확인: launchctl list | grep com.atlas"
echo "로그: tail -f $SCRIPT_DIR/../../logs/mcp_runner.log"
echo "해제: launchctl bootout gui/$(id -u) $TARGET"
