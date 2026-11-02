#!/usr/bin/env bash
# Empaqueta la app en un solo archivo ejecutable: dist/opl-manager
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cp "$DIR/oplcore.py" "$DIR/opl_manager.py" "$DIR/updater.py" "$TMP/"
printf 'import sys\nimport opl_manager\nsys.exit(opl_manager.App().run(sys.argv))\n' > "$TMP/__main__.py"
mkdir -p "$DIR/dist"
python3 -m zipapp "$TMP" -o "$DIR/dist/opl-manager" -p "/usr/bin/env python3" -c
chmod +x "$DIR/dist/opl-manager"
echo "✔ Ejecutable: $DIR/dist/opl-manager"
