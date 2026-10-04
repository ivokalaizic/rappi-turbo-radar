#!/bin/bash
# macOS (launchd): corre `python3 -m turbo run` cada 15 minutos y deja la UI (ui.py)
# siempre prendida en http://127.0.0.1:8765 (arranca con la sesión y se reinicia si se cae).
# Uso: scripts/launchd.sh install | uninstall | status
set -euo pipefail

LABEL="com.$(whoami).rappi-turbo"
UI_LABEL="$LABEL.ui"
AGENTS="$HOME/Library/LaunchAgents"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$(command -v python3)"

# write_plist <label> <claves extra> <argumentos...>
write_plist() {
  local label="$1" extra="$2"; shift 2
  local args=""
  for a in "$@"; do args+="<string>$a</string>"; done
  cat > "$AGENTS/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>$args</array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  $extra
  <key>StandardOutPath</key><string>$REPO/data/$label.out.log</string>
  <key>StandardErrorPath</key><string>$REPO/data/$label.err.log</string>
</dict>
</plist>
EOF
  launchctl unload "$AGENTS/$label.plist" 2>/dev/null || true
  launchctl load "$AGENTS/$label.plist"
}

case "${1:-}" in
  install)
    mkdir -p "$REPO/data" "$AGENTS"
    write_plist "$LABEL" "<key>StartInterval</key><integer>900</integer>
  <key>EnvironmentVariables</key><dict><key>TURBO_INTERVAL_S</key><string>900</string></dict>" \
      "$PYTHON" -m turbo run
    write_plist "$UI_LABEL" "<key>KeepAlive</key><true/>" "$PYTHON" "$REPO/ui.py" --no-browser
    echo "Instalado: corrida cada 15 min + UI en http://127.0.0.1:8765. Logs en $REPO/data/"
    ;;
  uninstall)
    for l in "$LABEL" "$UI_LABEL"; do
      launchctl unload "$AGENTS/$l.plist" 2>/dev/null || true
      rm -f "$AGENTS/$l.plist"
    done
    echo "Desinstalado."
    ;;
  status)
    launchctl list | grep "$LABEL" || echo "No está cargado."
    ;;
  *)
    echo "Uso: $0 install | uninstall | status"; exit 1 ;;
esac
