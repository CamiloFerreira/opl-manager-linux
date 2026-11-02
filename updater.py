"""
Actualizaciones automáticas desde los releases de GitHub.

Cómo se actualiza según cómo se instaló:
- "self":   ejecutable de un archivo en una carpeta del usuario (install.sh o descarga directa):
            se descarga el nuevo, se verifica su SHA256 y se reemplaza el archivo. Sin contraseña.
- "deb":    instalado con el paquete .deb en /usr/bin: se descarga el .deb, se verifica y se
            instala con pkexec (pide la contraseña en una ventana).
- "source": ejecutado desde el código (git clone): solo se avisa.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = "CamiloFerreira/opl-manager-linux"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY = 24 * 3600            # como máximo una consulta al día
UA = {"User-Agent": "opl-manager-linux-updater", "Accept": "application/vnd.github+json"}


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


def _get(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def latest_release():
    """Último release publicado: {'tag', 'version', 'url', 'notes', 'assets': {nombre: url}}."""
    data = json.loads(_get(API_LATEST))
    return {
        "tag": data["tag_name"],
        "version": data["tag_name"].lstrip("v"),
        "url": data["html_url"],
        "notes": data.get("body") or "",
        "assets": {a["name"]: a["browser_download_url"] for a in data.get("assets", [])},
    }


def should_check(cfg):
    return bool(cfg.get("auto_update", True)) and time.time() - float(cfg.get("last_update_check", 0)) > CHECK_EVERY


def _download_verified(rel, name, dest_dir):
    """Descarga un archivo del release y comprueba su SHA256 contra SHA256SUMS."""
    if name not in rel["assets"] or "SHA256SUMS" not in rel["assets"]:
        raise RuntimeError(f"El release no tiene {name} o SHA256SUMS")
    sums = _get(rel["assets"]["SHA256SUMS"]).decode()
    expected = None
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            expected = parts[0].lower()
    if not expected:
        raise RuntimeError(f"{name} no aparece en SHA256SUMS")
    data = _get(rel["assets"][name], timeout=120)
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("La descarga no coincide con su SHA256: no se instaló nada")
    path = os.path.join(dest_dir, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def apply_update(rel):
    """Instala el release. Devuelve 'restart' (listo, hay que reiniciar) o lanza una excepción."""
    mode = install_mode()
    if mode == "self":
        exe = executable_path()
        tmpdir = tempfile.mkdtemp(dir=os.path.dirname(exe), prefix=".opl-update-")
        try:
            new = _download_verified(rel, "opl-manager", tmpdir)
            os.chmod(new, 0o755)
            os.replace(new, exe)                      # reemplazo atómico
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return "restart"
    if mode == "deb":
        name = f"opl-manager_{rel['version']}_all.deb"
        tmpdir = tempfile.mkdtemp(prefix="opl-update-")
        try:
            deb = _download_verified(rel, name, tmpdir)
            os.chmod(tmpdir, 0o755)
            os.chmod(deb, 0o644)
            r = subprocess.run(["pkexec", "apt-get", "install", "-y", deb], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError("No se instaló el paquete" +
                                   (": se canceló la contraseña" if r.returncode in (126, 127) else
                                    f": {r.stderr.strip()[-300:]}"))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return "restart"
    raise RuntimeError("Esta copia se ejecuta desde el código: actualiza con git pull o descarga el release")


def restart():
    """Reemplaza el proceso actual por la versión nueva."""
    exe = executable_path() or os.path.abspath(sys.argv[0])
    if exe.startswith("/usr/") or not os.path.exists(exe):
        exe = shutil.which("opl-manager") or exe
    os.execv(sys.executable, [sys.executable, exe])
