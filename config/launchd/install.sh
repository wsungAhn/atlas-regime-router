#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

install_job() {
  local label="$1"
  local plist="$SCRIPT_DIR/$label.plist"
  local target="$HOME/Library/LaunchAgents/$label.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cp "$plist" "$target"
  if launchctl list | grep -q "$label"; then
    echo "[INFO] $label 기존 잡 언로드 후 재등록"
    launchctl bootout "gui/$(id -u)" "$target" 2>/dev/null || true
  fi
  launchctl bootstrap "gui/$(id -u)" "$target"
  echo "[OK] $label 등록 완료"
}

install_job "com.atlas.options-runner"
install_job "com.atlas.report-generator"

echo "options-runner: 9:30~16:00 ET 15분 간격, 장 열려있을 때만 실제 동작"
echo "report-generator: 13:10 PT(장마감 직후) 1회, reports/YYYY-MM-DD.md 생성"
echo "확인: launchctl list | grep com.atlas"
echo "로그: tail -f $SCRIPT_DIR/../../logs/mcp_runner.log"
echo "해제: launchctl bootout gui/\$(id -u) \$HOME/Library/LaunchAgents/<label>.plist"
