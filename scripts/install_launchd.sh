#!/usr/bin/env bash
# macOS の launchd に毎朝の定期実行を登録する。
#   ./scripts/install_launchd.sh          # 毎日 07:30 に実行
#   ./scripts/install_launchd.sh 6 45     # 毎日 06:45 に実行
#   ./scripts/install_launchd.sh uninstall
# スリープ中で実行時刻を逃した場合は、復帰後に1回実行される（StartCalendarInterval の仕様）。
set -euo pipefail

LABEL="com.${USER}.news-digest"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ "${1:-}" == "uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  rm -f "${PLIST}"
  echo "削除しました: ${LABEL}"
  exit 0
fi

HOUR="${1:-7}"
MINUTE="${2:-30}"
UV="$(command -v uv)" || { echo "uv が見つかりません" >&2; exit 1; }
mkdir -p "${PROJECT_DIR}/logs" "${HOME}/Library/LaunchAgents"

cat > "${PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>WorkingDirectory</key><string>${PROJECT_DIR}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${UV}</string><string>run</string><string>digest</string><string>run</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>${HOUR}</integer>
    <key>Minute</key><integer>${MINUTE}</integer>
  </dict>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(dirname "${UV}"):/usr/bin:/bin</string>
  </dict>
  <key>StandardOutPath</key><string>${PROJECT_DIR}/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>${PROJECT_DIR}/logs/launchd.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${PLIST}"
echo "登録しました: ${LABEL}（毎日 ${HOUR}:$(printf %02d "${MINUTE}")）"
echo "今すぐ試す: launchctl kickstart -k gui/$(id -u)/${LABEL}"
