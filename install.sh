#!/usr/bin/env bash
# Construye el ejecutable y lo instala para tu usuario:
#   ~/.local/bin/opl-manager  +  "OPL Manager" en el menú de aplicaciones.
# Desinstalar:  ./install.sh --quitar
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin/opl-manager"
DESK="$HOME/.local/share/applications/io.github.camiloferreira.OplManager.desktop"

# entrada de menú de versiones anteriores (otro identificador)
rm -f -- "$HOME/.local/share/applications/io.github.dotnginx.OplManager.desktop"

if [[ "${1:-}" == "--quitar" ]]; then
  rm -f -- "$BIN" "$DESK"
  echo "✔ Desinstalado (tu configuración en ~/.config/opl-manager se conserva)."
  exit 0
fi

"$DIR/build.sh"
mkdir -p "$(dirname "$BIN")" "$(dirname "$DESK")"
install -m 755 "$DIR/dist/opl-manager" "$BIN"
cat > "$DESK" <<DESKTOP
[Desktop Entry]
Type=Application
Name=OPL Manager
Comment=Gestor de juegos de PS2 para Open PS2 Loader
Exec=$BIN
Icon=media-optical
Terminal=false
Categories=Game;Utility;
StartupWMClass=io.github.camiloferreira.OplManager
DESKTOP
update-desktop-database "$(dirname "$DESK")" 2>/dev/null || true
echo "✔ Instalado: $BIN"
echo "  Ábrelo desde el menú (\"OPL Manager\") o escribiendo: opl-manager"
