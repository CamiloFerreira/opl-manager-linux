#!/usr/bin/env bash
# Construye los archivos de un release en dist/release/:
#   opl-manager                       ejecutable de un archivo (zipapp de Python)
#   opl-manager_<versión>_all.deb     paquete para Ubuntu/Debian (instala dependencias con apt)
#   opl-manager-linux-<versión>.tar.gz  código fuente con install.sh
#   release.json                      manifiesto (versión, versión mínima segura)
#   SHA256SUMS + SHA256SUMS.sig       sumas y su firma Ed25519 (si hay clave)
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

# ---- 4) manifiesto: versión y versión mínima segura (las anteriores deben actualizarse sí o sí)
python3 - "$VERSION" "$ROOT/packaging/security.json" "$OUT/release.json" <<'PY'
import json, re, sys
version, policy, out = sys.argv[1:]
sec = json.load(open(policy, encoding="utf-8"))
msv = sec.get("min_safe_version", "0.0.0")
assert re.fullmatch(r"\d+\.\d+\.\d+", msv), "min_safe_version no válida"
key = lambda v: tuple(int(x) for x in v.split("."))
assert key(msv) <= key(version), "min_safe_version no puede ser mayor que la versión publicada"
json.dump({"version": version, "min_safe_version": msv, "advisory": sec.get("advisory", "")},
          open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY

# ---- 5) sumas de verificación y firma Ed25519 (la app no instala nada sin firma válida)
( cd "$OUT" && sha256sum -- * > SHA256SUMS )
if [[ -n "${OPL_SIGNING_KEY_FILE:-}" ]]; then
  openssl pkeyutl -sign -inkey "$OPL_SIGNING_KEY_FILE" -rawin -in "$OUT/SHA256SUMS" -out "$OUT/SHA256SUMS.sig"
  python3 "$ROOT/updater.py" verify "$OUT/SHA256SUMS" "$OUT/SHA256SUMS.sig"   # con la clave pública de la app
elif [[ "${OPL_REQUIRE_SIGNATURE:-0}" == "1" ]]; then
  echo "::error::Falta la clave de firma (OPL_SIGNING_KEY_FILE): no se publica un release sin firmar" >&2
  exit 1
else
  echo "(sin firmar: build de prueba)"
fi

# ---- 6) notas del release
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

Verificar la firma (clave pública Ed25519 del proyecto en \`updater.py\`): \`python3 updater.py verify SHA256SUMS SHA256SUMS.sig\`
o la procedencia del build: \`gh attestation verify opl-manager --repo CamiloFerreira/opl-manager-linux\`
EOF
python3 - "$OUT/release.json" "$ROOT/dist/release-notes.md" <<'PY'
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
if m["min_safe_version"] == m["version"] and m.get("advisory"):
    notes = open(sys.argv[2], encoding="utf-8").read()
    head = ("## ⚠️ Actualización de seguridad obligatoria\n\n" + m["advisory"] +
            "\n\nLas versiones anteriores a " + m["version"] + " deben actualizarse.\n\n")
    open(sys.argv[2], "w", encoding="utf-8").write(head + notes)
PY

echo "== Archivos en dist/release:"
ls -la "$OUT"
