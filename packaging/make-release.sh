#!/usr/bin/env bash
# Construye los archivos de un release en dist/release/:
#   opl-manager                       ejecutable de un archivo (zipapp de Python)
#   opl-manager_<versión>_all.deb     paquete para Ubuntu/Debian (instala dependencias con apt)
#   opl-manager-linux-<versión>.tar.gz  código fuente con install.sh
#   SHA256SUMS
# y dist/release-notes.md con las notas del release.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="$(python3 -c 'import re; print(re.search(r"__version__ = \"([^\"]+)\"", open("oplcore.py").read())[1])')"
APP_ID="io.github.camiloferreira.OplManager"
OUT="$ROOT/dist/release"
rm -rf "$OUT" && mkdir -p "$OUT"
echo "== OPL Manager $VERSION"

# ---- 1) ejecutable de un archivo
"$ROOT/build.sh" >/dev/null
cp "$ROOT/dist/opl-manager" "$OUT/opl-manager"
chmod 755 "$OUT/opl-manager"

# ---- 2) paquete .deb
PKG="$(mktemp -d)"; trap 'rm -rf "$PKG"' EXIT; chmod 755 "$PKG"
install -Dm755 "$OUT/opl-manager" "$PKG/usr/bin/opl-manager"
install -Dm644 "$ROOT/packaging/$APP_ID.desktop" "$PKG/usr/share/applications/$APP_ID.desktop"
install -Dm644 "$ROOT/LICENSE" "$PKG/usr/share/doc/opl-manager/copyright"
install -Dm644 "$ROOT/README.md" "$PKG/usr/share/doc/opl-manager/README.md"
SIZE_KB="$(du -sk "$PKG/usr" | cut -f1)"
mkdir -p "$PKG/DEBIAN"
cat > "$PKG/DEBIAN/control" <<EOF
Package: opl-manager
Version: $VERSION
Section: games
Priority: optional
Architecture: all
Installed-Size: $SIZE_KB
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), python3-pil
Maintainer: CamiloFerreira <31254436+CamiloFerreira@users.noreply.github.com>
Homepage: https://github.com/CamiloFerreira/opl-manager-linux
Description: Gestor de bibliotecas de Open PS2 Loader (OPL)
 Organiza los juegos de PS2 para Open PS2 Loader: biblioteca con portadas,
 descarga del arte de OPL, nombres en formato OPL, detección de duplicados
 y copia desde tus carpetas de ROMs al pendrive o disco de la PS2.
EOF
DEB="$OUT/opl-manager_${VERSION}_all.deb"
if command -v fakeroot >/dev/null; then
  fakeroot dpkg-deb --build -Zxz "$PKG" "$DEB" >/dev/null
else
  dpkg-deb --root-owner-group --build -Zxz "$PKG" "$DEB" >/dev/null
fi

# ---- 3) código fuente
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$ROOT" archive --format=tar.gz --prefix="opl-manager-linux-$VERSION/" \
      -o "$OUT/opl-manager-linux-$VERSION.tar.gz" HEAD
else
  tar -czf "$OUT/opl-manager-linux-$VERSION.tar.gz" --exclude=dist --exclude=.git --exclude=__pycache__ \
      --transform "s,^\.,opl-manager-linux-$VERSION," -C "$ROOT" .
fi

# ---- 4) sumas de verificación
( cd "$OUT" && sha256sum -- * > SHA256SUMS )

# ---- 5) notas del release
cat > "$ROOT/dist/release-notes.md" <<EOF
## Instalar

**Ubuntu / Debian / Linux Mint / Pop!_OS** (recomendado: instala las dependencias solo)
\`\`\`bash
sudo apt install ./opl-manager_${VERSION}_all.deb
\`\`\`

**Cualquier distro** — ejecutable de un archivo. Requiere Python 3.10+, GTK 4, libadwaita 1.5+, PyGObject y Pillow
(Fedora: \`sudo dnf install python3-gobject gtk4 libadwaita python3-pillow\`;
Arch: \`sudo pacman -S python-gobject gtk4 libadwaita python-pillow\`).
\`\`\`bash
chmod +x opl-manager
./opl-manager
\`\`\`

**Desde el código:** descomprime \`opl-manager-linux-${VERSION}.tar.gz\` y ejecuta \`./install.sh\`.

Verificar las descargas: \`sha256sum -c SHA256SUMS --ignore-missing\`
EOF

echo "== Archivos en dist/release:"
ls -la "$OUT"
