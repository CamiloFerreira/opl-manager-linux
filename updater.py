"""
Actualizaciones automáticas desde los releases de GitHub.

Cómo se actualiza según cómo se instaló:
- "self":   ejecutable de un archivo en una carpeta del usuario (install.sh o descarga directa):
            se descarga el nuevo, se verifica y se reemplaza el archivo. Sin contraseña.
- "deb":    instalado con el paquete .deb en /usr/bin: se descarga el .deb, se verifica y se
            instala con pkexec (pide la contraseña en una ventana).
- "source": ejecutado desde el código (git clone): solo se avisa.

Cadena de confianza (autenticidad + integridad):
  SHA256SUMS.sig  firma Ed25519 de SHA256SUMS hecha en el CI con la clave del proyecto.
                  La clave pública está fija en este archivo (RELEASE_PUBKEY): aunque alguien
                  suba archivos falsos a un release, no puede firmarlos.
  SHA256SUMS      sumas de todos los archivos del release (incluido release.json).
  release.json    manifiesto firmado: versión y "min_safe_version". Si la versión instalada es
                  menor que min_safe_version, la actualización es OBLIGATORIA (corrige
                  vulnerabilidades) y se instala aunque el usuario haya desactivado las automáticas.
Nada se instala si falta la firma, si no es válida o si un archivo no coincide con su suma.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404 - solo se ejecuta pkexec/apt-get con argumentos fijos
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = "CamiloFerreira/opl-manager-linux"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_PAGE_PREFIX = f"https://github.com/{REPO}/releases/"
ASSET_PREFIX = f"https://github.com/{REPO}/releases/download/"
CHECK_EVERY = 24 * 3600            # como máximo una consulta al día
UA = {"User-Agent": "opl-manager-linux-updater", "Accept": "application/vnd.github+json"}

# Clave pública Ed25519 de los releases (la privada solo existe como secreto del CI).
RELEASE_PUBKEY = bytes.fromhex("49fad8072ae37d304fb8475186ee5730bda5fc39609b1ba954bde5eb01ff08cf")

TAG_RE = re.compile(r"^v(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
# límites de tamaño de las descargas (evita agotar la memoria con respuestas enormes)
MAX_API = 2 * 1024 * 1024
MAX_SMALL = 256 * 1024             # SHA256SUMS, firma, release.json
MAX_ASSET = 200 * 1024 * 1024      # ejecutable o .deb


class UpdateError(RuntimeError):
    pass


# ---------------------------------------------------------------- Ed25519 (RFC 8032, solo verificación)
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _padd(a, b):
    A = (a[1] - a[0]) * (b[1] - b[0]) % _P
    B = (a[1] + a[0]) * (b[1] + b[0]) % _P
    C = 2 * a[3] * b[3] * _D % _P
    D = 2 * a[2] * b[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _pmul(s, pt):
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _padd(q, pt)
        pt = _padd(pt, pt)
        s >>= 1
    return q


def _peq(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_G = (_recover_x(_GY, 0), _GY, 1, _recover_x(_GY, 0) * _GY % _P)


def ed25519_verify(public, msg, sig):
    """True si sig es una firma Ed25519 válida de msg con la clave pública (32 bytes)."""
    try:   # si está instalada, la implementación de 'cryptography' es más rápida
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(sig, msg)
            return True
        except (InvalidSignature, ValueError):
            return False
    except ImportError:
        pass
    if len(public) != 32 or len(sig) != 64:
        return False
    a = _decompress(public)
    r = _decompress(sig[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + public + msg).digest(), "little") % _L
    return _peq(_pmul(s, _G), _padd(r, _pmul(h, a)))


# ---------------------------------------------------------------- versiones
def parse_version(v):
    """'v0.10.2' -> (0, 10, 2). Partes no numéricas cuentan como 0."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def is_newer(remote, local):
    return parse_version(remote) > parse_version(local)


def executable_path():
    """Ruta del ejecutable de un archivo (zipapp) que está corriendo, o '' si se ejecuta desde el código."""
    here = os.path.dirname(os.path.abspath(__file__))
    return here if os.path.isfile(here) else ""


def install_mode():
    exe = executable_path()
    if not exe:
        return "source"
    if os.access(exe, os.W_OK) and os.access(os.path.dirname(exe), os.W_OK):
        return "self"
    if exe.startswith("/usr/") and shutil.which("pkexec") and shutil.which("apt-get"):
        return "deb"
    return "source"


# ---------------------------------------------------------------- red (solo HTTPS)
class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise UpdateError(f"Redirección insegura rechazada: {newurl[:80]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnlyRedirect)


def _get(url, limit, timeout=20):
    if not url.lower().startswith("https://"):
        raise UpdateError("Solo se permiten descargas por HTTPS")
    req = urllib.request.Request(url, headers=UA)
    with _opener.open(req, timeout=timeout) as r:  # nosec B310 - esquema validado arriba
        data = r.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("La respuesta es demasiado grande")
    return data


# ---------------------------------------------------------------- release
def _parse_sums(text):
    sums = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            sums[parts[1].lstrip("*")] = parts[0].lower()
    return sums


def latest_release():
    """Último release publicado y verificado:
    {'tag', 'version', 'url', 'notes', 'assets', 'sums', 'mandatory', 'min_safe_version', 'advisory'}.
    Lanza UpdateError si la respuesta no es válida o la firma no se puede verificar."""
    data = json.loads(_get(API_LATEST, MAX_API))
    tag = str(data.get("tag_name", ""))
    if not TAG_RE.match(tag):
        raise UpdateError(f"Tag de release no válido: {tag[:40]!r}")
    url = str(data.get("html_url", ""))
    if not url.startswith(RELEASE_PAGE_PREFIX):
        url = RELEASE_PAGE_PREFIX + "latest"
    assets = {}
    for a in data.get("assets", []):
        name, link = str(a.get("name", "")), str(a.get("browser_download_url", ""))
        if re.fullmatch(r"[A-Za-z0-9._+-]{1,128}", name) and link.startswith(ASSET_PREFIX):
            assets[name] = link
    rel = {"tag": tag, "version": tag[1:], "url": url, "notes": str(data.get("body") or "")[:20000],
           "assets": assets}
    verify_release(rel)
    return rel


def verify_release(rel):
    """Descarga SHA256SUMS + firma + release.json y los comprueba. Completa rel con
    'sums', 'mandatory', 'min_safe_version' y 'advisory'."""
    for need in ("SHA256SUMS", "SHA256SUMS.sig", "release.json"):
        if need not in rel["assets"]:
            raise UpdateError(f"El release no está firmado (falta {need}): no se instalará")
    sums_raw = _get(rel["assets"]["SHA256SUMS"], MAX_SMALL)
    sig = _get(rel["assets"]["SHA256SUMS.sig"], MAX_SMALL)
    if not ed25519_verify(RELEASE_PUBKEY, sums_raw, sig):
        raise UpdateError("La firma del release no es válida: no se instalará nada")
    sums = _parse_sums(sums_raw.decode("utf-8", "replace"))
    manifest_raw = _get(rel["assets"]["release.json"], MAX_SMALL)
    if hashlib.sha256(manifest_raw).hexdigest() != sums.get("release.json"):
        raise UpdateError("release.json no coincide con SHA256SUMS")
    manifest = json.loads(manifest_raw)
    if manifest.get("version") != rel["version"]:
        raise UpdateError("La versión del manifiesto firmado no coincide con el tag")
    msv = str(manifest.get("min_safe_version") or "0.0.0")
    if not VERSION_RE.match(msv):
        msv = "0.0.0"
    rel.update(sums=sums, min_safe_version=msv, advisory=str(manifest.get("advisory") or "")[:2000])
    return rel


def is_mandatory(rel, local_version):
    """La actualización es obligatoria si la versión instalada tiene vulnerabilidades conocidas."""
    return is_newer(rel.get("min_safe_version", "0.0.0"), local_version) and is_newer(rel["version"], local_version)


def should_check(cfg):
    """Se consulta como máximo una vez al día. Se consulta incluso con 'auto_update' apagado:
    en ese caso solo se actúa si la actualización es de seguridad (obligatoria)."""
    if cfg.get("security_update_pending"):
        return True
    return time.time() - float(cfg.get("last_update_check", 0)) > CHECK_EVERY


def _download_verified(rel, name, dest_dir):
    """Descarga un archivo del release y comprueba su SHA256 contra SHA256SUMS (ya firmado)."""
    if not re.fullmatch(r"[A-Za-z0-9._+-]{1,128}", name) or name not in rel["assets"]:
        raise UpdateError(f"El release no tiene {name}")
    expected = rel.get("sums", {}).get(name)
    if not expected:
        raise UpdateError(f"{name} no aparece en SHA256SUMS firmado")
    data = _get(rel["assets"][name], MAX_ASSET, timeout=120)
    if hashlib.sha256(data).hexdigest() != expected:
        raise UpdateError("La descarga no coincide con su SHA256: no se instaló nada")
    path = os.path.join(dest_dir, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def apply_update(rel, local_version=None):
    """Instala el release. Devuelve 'restart' (listo, hay que reiniciar) o lanza una excepción."""
    if not VERSION_RE.match(rel.get("version", "")):
        raise UpdateError("Versión no válida")
    if local_version and not is_newer(rel["version"], local_version):
        raise UpdateError("No se instalan versiones anteriores o iguales a la actual")
    if "sums" not in rel:
        verify_release(rel)
    mode = install_mode()
    if mode == "self":
        exe = executable_path()
        tmpdir = tempfile.mkdtemp(dir=os.path.dirname(exe), prefix=".opl-update-")
        try:
            new = _download_verified(rel, "opl-manager", tmpdir)
            os.chmod(new, 0o755)  # nosec B103 - ejecutable del usuario
            os.replace(new, exe)                      # reemplazo atómico
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return "restart"
    if mode == "deb":
        name = f"opl-manager_{rel['version']}_all.deb"
        tmpdir = tempfile.mkdtemp(prefix="opl-update-")
        try:
            deb = _download_verified(rel, name, tmpdir)
            os.chmod(deb, 0o444)
            os.chmod(tmpdir, 0o555)  # nosec B103 - apt (_apt) necesita leerlo; nadie puede escribir
            pkexec, apt = shutil.which("pkexec"), shutil.which("apt-get")
            if not pkexec or not apt:
                raise UpdateError("Falta pkexec o apt-get")
            r = subprocess.run([pkexec, apt, "install", "-y", "--", deb],  # nosec B603
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise UpdateError("No se instaló el paquete" +
                                  (": se canceló la contraseña" if r.returncode in (126, 127) else
                                   f": {r.stderr.strip()[-300:]}"))
        finally:
            try:
                os.chmod(tmpdir, 0o700)
            except OSError:
                pass
            shutil.rmtree(tmpdir, ignore_errors=True)
        return "restart"
    raise UpdateError("Esta copia se ejecuta desde el código: actualiza con git pull o descarga el release")


def restart():
    """Reemplaza el proceso actual por la versión nueva."""
    exe = executable_path() or os.path.abspath(sys.argv[0])
    if exe.startswith("/usr/") or not os.path.exists(exe):
        exe = shutil.which("opl-manager") or exe
    os.execv(sys.executable, [sys.executable, exe])  # nosec B606


if __name__ == "__main__":
    # Uso en el CI: python3 updater.py verify SHA256SUMS SHA256SUMS.sig
    if len(sys.argv) == 4 and sys.argv[1] == "verify":
        with open(sys.argv[2], "rb") as fm, open(sys.argv[3], "rb") as fs:
            ok = ed25519_verify(RELEASE_PUBKEY, fm.read(), fs.read())
        print("firma válida" if ok else "FIRMA NO VÁLIDA")
        sys.exit(0 if ok else 1)
    print(__doc__)
