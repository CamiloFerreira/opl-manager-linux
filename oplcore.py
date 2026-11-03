"""
Lógica de OPL Manager para Linux (sin interfaz).

- Lee el código del juego (SLUS_209.46...) y la consola desde el SYSTEM.CNF del disco.
- Arma nombres en formato OPL:  CÓDIGO.Nombre corto.iso  (máx. 32 caracteres, sin acentos).
- Escanea la biblioteca OPL (DVD/, CD/, POPS/, ART/) y la bandeja de entrada (descargas).
- Copia juegos a la biblioteca y descarga el arte (portada, lomo, fondo, capturas...).

Regla de seguridad: en la biblioteca nunca se borra ni se sobrescribe nada.
"""
import json
import os
import re
import shutil
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import pops

__version__ = "0.6.0"

# ---------------------------------------------------------------- constantes
ID_RE = re.compile(r"([A-Z]{4})[_-](\d{3})\.(\d{2})")
ID_PREFIX_RE = re.compile(r"^([A-Z]{4})[_-](\d{3})\.(\d{2})[._ -]")
GID_RE = re.compile(r"^[A-Z]{4}_\d{3}\.\d{2}$")      # código ya normalizado: SLUS_209.46
MAX_ART_BYTES = 16 * 1024 * 1024                       # límite por imagen descargada

ROM_EXT = {"iso", "bin", "cue", "img", "chd", "cso", "zso", "mdf", "mds", "nrg", "ecm", "pbp", "ccd", "sub", "m3u"}
SOLO_EXT = {"iso", "chd", "cso", "zso", "pbp", "nrg"}           # un solo archivo
DATA_EXT = {"iso", "bin", "img", "mdf", "nrg", "chd", "cso", "zso", "pbp", "ecm"}
COVER_RE = re.compile(r"(cover|portada|car[aá]tula)", re.I)
IMG_EXT = {"png", "jpg", "jpeg", "webp"}

# {console} = PS2 o PS1 (la base tiene una carpeta para cada consola)
ART_BASE = "https://raw.githubusercontent.com/Luden02/psx-ps2-opl-art-database/main/{console}/{id}/{id}_{t}.png"
# tipo en OPL -> nombres en la base de arte (se usa el primero que exista)
ART_TYPES = {
    "COV": ["COV"], "COV2": ["COV2"], "LAB": ["LAB"], "LGO": ["LGO"], "ICO": ["ICO"],
    "BG": ["BG_00", "BG"], "SCR": ["SCR_00", "SCR"], "SCR2": ["SCR_01", "SCR2"],
}
ART_LABELS = {
    "COV": "Portada", "COV2": "Contraportada", "LAB": "Lomo", "LGO": "Logo",
    "ICO": "Ícono", "BG": "Fondo", "SCR": "Captura 1", "SCR2": "Captura 2",
}
RECENT_SECONDS = 180      # carpetas modificadas hace menos de esto: "se está extrayendo"

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "opl-manager")
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "opl-manager")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
ID_CACHE_FILE = os.path.join(CACHE_DIR, "ids.json")

def _downloads_dir():
    """Carpeta de descargas del usuario (XDG), p. ej. ~/Descargas o ~/Downloads."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".config", "user-dirs.dirs"), encoding="utf-8") as f:
            m = re.search(r'^XDG_DOWNLOAD_DIR="([^"]+)"', f.read(), re.M)
            if m:
                return m[1].replace("$HOME", os.path.expanduser("~"))
    except OSError:
        pass
    return os.path.join(os.path.expanduser("~"), "Downloads")


DEFAULT_CONFIG = {
    "inboxes": [_downloads_dir()],   # carpetas de ROMs a revisar (bandeja de entrada)
    "library": "",              # vacío: al abrir se busca sola la unidad con OPL
    "ps1_mode": "vcd",          # PS1: "vcd" = convertir a POPS/*.VCD · "copy" = copiar los .bin/.cue a ps1_dir
    "pops_apps": True,          # PS1: crear APPS/<juego>/XX.<juego>.ELF + title.cfg para verlos en OPL
    "ps1_dir": "CD",            # carpeta de la biblioteca donde van los .bin/.cue de PS1 (modo "copy")
    "max_name": 32,             # 0 = sin límite (OPL nuevo)
    "download_art": True,
    "sort": 0,                  # 0 nombre, 1 código, 2 tamaño, 3 más recientes
    "trash_after_copy": False,  # mover a la papelera lo copiado de la bandeja
    "background_on_close": True,  # al cerrar durante una copia, seguir en segundo plano
    "close_to_tray": True,      # al cerrar, quedarse en la bandeja del sistema (si existe)
    "auto_update": True,        # buscar versiones nuevas al abrir (máx. 1 vez al día)
    "auto_install": True,       # instalarlas solas cuando no hace falta contraseña
    "last_update_check": 0,
}


def ext_of(path):
    return path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else ""


# ---------------------------------------------------------------- configuración
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        cfg.update(saved)
        if "inboxes" not in saved and saved.get("inbox"):      # migrar config vieja
            cfg["inboxes"] = [saved["inbox"]]
    except (OSError, ValueError):
        pass
    cfg["inboxes"] = [p for p in dict.fromkeys(cfg.get("inboxes") or []) if p]
    return cfg


def valid_gid(gid):
    """El código de juego tiene la forma exacta SLUS_209.46 (se usa para armar rutas y URLs)."""
    return bool(gid) and bool(GID_RE.match(gid))


def save_config(cfg):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)


# ---------------------------------------------------------------- lectura del disco
_id_cache = None


def _cache():
    global _id_cache
    if _id_cache is None:
        try:
            with open(ID_CACHE_FILE, encoding="utf-8") as f:
                _id_cache = json.load(f)
        except (OSError, ValueError):
            _id_cache = {}
    return _id_cache


def _save_cache():
    try:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        tmp = ID_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache(), f)
        os.replace(tmp, ID_CACHE_FILE)
    except OSError:
        pass


def _read_boot_line(path):
    """Línea BOOT/BOOT2 del SYSTEM.CNF, buscando el archivo en el índice ISO9660
    (2048 o 2352 bytes por sector). Si falla, busca el texto en los primeros 8 MB.
    En un .VCD de POPS el disco empieza después de la cabecera de 1 MiB."""
    base = pops.HEADER if ext_of(path) == "vcd" else 0
    try:
        f = open(path, "rb")
    except OSError:
        return ""
    with f:
        def sec(lba, size, off):
            f.seek(base + lba * size + off)
            return f.read(2048)
        try:
            for size, off in ((2048, 0), (2352, 24), (2352, 16)):
                pvd = sec(16, size, off)
                if pvd[1:6] != b"CD001":
                    continue
                root = pvd[156:190]
                lba = int.from_bytes(root[2:6], "little")
                ln = int.from_bytes(root[10:14], "little")
                data = b"".join(sec(lba + i, size, off) for i in range(min((ln + 2047) // 2048, 64)))
                i = 0
                while i < len(data):
                    n = data[i]
                    if n == 0:
                        i = (i // 2048 + 1) * 2048
                        continue
                    if n < 34 or i + 33 > len(data):      # registro dañado: se deja de leer el índice
                        break
                    name = data[i + 33:i + 33 + data[i + 32]].split(b";")[0].upper()
                    if name == b"SYSTEM.CNF":
                        elba = int.from_bytes(data[i + 2:i + 6], "little")
                        txt = sec(elba, size, off).decode("latin-1")
                        m = re.search(r"BOOT2? *= *cdrom0?:[^\r\n;]*", txt)
                        if m:
                            return m.group(0)
                    i += n
                break
            f.seek(base)
            m = re.search(rb"BOOT2? *= *cdrom0?:[^\r\n;]{0,40}", f.read(8_000_000))
            return m.group(0).decode("latin-1") if m else ""
        except (OSError, ValueError, IndexError, OverflowError):
            return ""


def disc_info(path):
    """(consola, código) del disco: ('PS2'|'PS1'|'', 'SLUS_209.46'|''). Con caché."""
    try:
        st = os.stat(path)
    except OSError:
        return "", ""
    key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
    c = _cache()
    hit = c.get(key)
    if isinstance(hit, list) and len(hit) == 2 and hit[0] in ("PS2", "PS1", "") and \
            (hit[1] == "" or valid_gid(hit[1])):
        return hit[0], hit[1]
    line = _read_boot_line(path)
    console = "PS2" if line.startswith("BOOT2") else ("PS1" if line.startswith("BOOT") else "")
    m = ID_RE.search(line)
    gid = f"{m[1]}_{m[2]}.{m[3]}" if m else ""
    c[key] = [console, gid]
    _save_cache()
    return console, gid


def id_from_name(filename):
    m = ID_PREFIX_RE.match(filename)
    return f"{m[1]}_{m[2]}.{m[3]}" if m else ""


def strip_id(stem):
    m = ID_PREFIX_RE.match(stem + ".")
    return stem[m.end():] if m and len(stem) >= m.end() else stem


# ---------------------------------------------------------------- nombres OPL
_TAG_END = re.compile(r"\s*[\[({][^\])}]*[\])}]\s*$")
_TAG_START = re.compile(r"^\s*[\[({][^\])}]*[\])}]\s*")
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def ascii_name(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.encode("ascii", "ignore").decode()


def short_title(title, max_len=32):
    """Título apto para OPL viejo: sin acentos, sin etiquetas sobrantes, máx. max_len caracteres."""
    t = _BAD_CHARS.sub("", title.replace("_", " "))
    if max_len:
        t = ascii_name(t)
    t = re.sub(r"\s+", " ", t).strip()
    fallback = "Juego"
    if not max_len or len(t) <= max_len:
        return t.rstrip(" .-_,") or fallback
    while len(t) > max_len:
        new = _TAG_END.sub("", t)
        new = _TAG_START.sub("", new)
        if new == t or not new:
            break
        t = new
    if len(t) > max_len:
        cut = t[:max_len + 1]
        t = cut.rsplit(" ", 1)[0] if " " in cut else t[:max_len]
    return t.rstrip(" .-_,") or fallback


def opl_filename(gid, title, ext, max_len=32):
    """Nombre de archivo (nunca una ruta: sin '/', sin '..', sin empezar con '.')."""
    t = short_title(title, max_len).lstrip(". ") or "Juego"
    ext = re.sub(r"[^A-Za-z0-9]", "", ext or "")[:8] or "iso"
    gid = gid if valid_gid(gid) else ""
    name = f"{gid}.{t}.{ext}" if gid else f"{t}.{ext}"
    if os.sep in name or name in (".", "..") or "\x00" in name:
        raise ValueError(f"Nombre de archivo no válido: {name!r}")
    return name


def free_path(path):
    """Ruta que no existe: agrega ' (2)', ' (3)'... si hace falta."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit not in ("B", "KB") else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n} B"


# ---------------------------------------------------------------- biblioteca
@dataclass
class Game:
    path: str
    kind: str            # 'DVD' | 'CD' | 'POPS'
    gid: str
    title: str
    size: int
    library: str
    mtime: float = 0.0

    @property
    def filename(self):
        return os.path.basename(self.path)

    def art(self, t):
        if not self.gid:
            return ""
        p = os.path.join(self.library, "ART", f"{self.gid}_{t}.png")
        return p if os.path.exists(p) else ""

    @property
    def console(self):
        """Consola para la base de arte: los juegos de POPS son de PS1."""
        return "PS1" if self.kind == "POPS" else "PS2"

    def missing_art(self):
        return [t for t in ART_TYPES if not self.art(t)] if self.gid else []


def scan_library(root):
    games = []
    for kind, exts in (("DVD", {"iso"}), ("CD", {"iso"}), ("POPS", {"vcd"})):
        d = os.path.join(root, kind)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d), key=str.lower):
            p = os.path.join(d, name)
            if not os.path.isfile(p) or ext_of(name) not in exts:
                continue
            stem = os.path.splitext(name)[0]
            gid = id_from_name(name)
            if not gid:
                gid = disc_info(p)[1]
            try:
                st = os.stat(p)
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = 0, 0.0
            games.append(Game(p, kind, gid, strip_id(stem), size, root, mtime))
    return games


SORT_NAMES = ["Nombre (A-Z)", "Código", "Tamaño", "Más recientes"]


def sort_key(game, mode):
    title = ascii_name(game.title).lower()
    if mode == 1:
        return (game.gid or "~", title)
    if mode == 2:
        return (-game.size, title)
    if mode == 3:
        return (-game.mtime, title)
    return (title, game.gid)


def find_duplicates(games):
    """Grupos de juegos de PS2 con el mismo código (2 o más archivos).
    El primero de cada grupo es el que se sugiere conservar: nombre OPL (con código),
    luego el más grande, luego el más reciente."""
    by_id = {}
    for g in games:
        if g.gid and g.kind in ("DVD", "CD"):
            by_id.setdefault(g.gid, []).append(g)
    groups = []
    for gid, gs in by_id.items():
        if len(gs) > 1:
            gs.sort(key=lambda g: (not id_from_name(g.filename), -g.size, -g.mtime))
            groups.append(gs)
    groups.sort(key=lambda gs: ascii_name(gs[0].title).lower())
    return groups


def _move(src, dst):
    """os.rename que también sirve para cambiar solo mayúsculas/minúsculas en FAT32/exFAT
    (ahí 'juego.iso' y 'Juego.iso' son el mismo archivo y exists() da True)."""
    if os.path.exists(dst):
        same = src.lower() == dst.lower() and os.path.samefile(src, dst)
        if not same:
            raise FileExistsError(f"Ya existe: {os.path.basename(dst)}")
        tmp = free_path(src + ".renombrando")
        os.rename(src, tmp)
        src = tmp
    os.rename(src, dst)


def rename_to(game, new_name):
    """Renombra el archivo del juego a new_name (solo el nombre, nunca una ruta). No borra nada.
    En POPS también renombra la carpeta de partidas guardadas (POPS/<nombre sin .VCD>/)."""
    if not new_name or os.sep in new_name or new_name in (".", "..") or "\x00" in new_name:
        raise ValueError(f"Nombre de archivo no válido: {new_name!r}")
    folder = os.path.dirname(game.path)
    new = os.path.join(folder, new_name)
    if new == game.path:
        return new
    _move(game.path, new)
    if game.kind == "POPS":
        old_dir = os.path.splitext(game.path)[0]
        new_dir = os.path.splitext(new)[0]
        if os.path.isdir(old_dir) and not os.path.islink(old_dir):
            try:
                _move(old_dir, new_dir)
            except OSError:
                pass        # la carpeta de destino ya existe: se deja todo como estaba
        try:
            _rename_pops_app(game.library, game.path, new)
        except OSError:
            pass
    return new


def rename_game(game, new_title, max_len=32):
    """Renombra el archivo del juego (no borra nada). Devuelve la nueva ruta."""
    ext = os.path.splitext(game.path)[1][1:]          # se conserva tal cual (p. ej. .VCD en mayúsculas)
    return rename_to(game, opl_filename(game.gid, new_title, ext, max_len))


# código metido en el medio del nombre: "God of War [SLUS-20678]", "(SLES_533.66)"
_EMBEDDED_ID = re.compile(r"[\[({]?\s*\b[A-Z]{4}[_-]\d{3}\.?\d{2}\b\s*[\])}]?")


def clean_title(title):
    """Título sin el código del juego repetido adentro (ya va al principio del nombre)."""
    t = _EMBEDDED_ID.sub(" ", title)
    t = re.sub(r"[\[({]\s*[\])}]", " ", t)
    t = re.sub(r"\s+", " ", t.replace("_", " ")).strip(" .-_,")
    return t or title


@dataclass
class NameFix:
    game: Game
    new_name: str
    problem: str = ""     # si no se puede renombrar (p. ej. ya existe un archivo con ese nombre)


def name_fixes(games, max_len=32):
    """Juegos cuyo archivo no sigue el formato de OPL (CÓDIGO.Nombre.iso) y el nombre que les corresponde."""
    fixes, planned = [], set()
    for g in games:
        ext = os.path.splitext(g.path)[1][1:]
        try:
            new = opl_filename(g.gid, clean_title(g.title), ext, max_len)
        except ValueError:
            continue
        if new == g.filename:
            continue
        dst = os.path.join(os.path.dirname(g.path), new)
        key = dst.lower()                     # FAT32/exFAT no distinguen mayúsculas
        problem = ""
        if key in planned:
            problem = "Otro juego quedaría con el mismo nombre"
        elif os.path.exists(dst) and not (key == g.path.lower() and os.path.samefile(dst, g.path)):
            problem = "Ya existe un archivo con ese nombre"
        else:
            planned.add(key)
        fixes.append(NameFix(g, new, problem))
    return fixes


def apply_name_fixes(fixes, progress=None):
    """Renombra los juegos de la lista. Devuelve (renombrados, [errores])."""
    done, errors = 0, []
    for i, f in enumerate(fixes):
        if progress:
            progress(i, len(fixes), f.game.filename)
        if f.problem:
            errors.append(f"{f.game.filename}: {f.problem}")
            continue
        try:
            rename_to(f.game, f.new_name)
            done += 1
        except (OSError, ValueError) as e:
            errors.append(f"{f.game.filename}: {e}")
    return done, errors


def save_cover(library, gid, image_path, overwrite=False):
    """Guarda una imagen como ART/<código>_COV.png (140x200)."""
    from PIL import Image
    if not valid_gid(gid):
        raise ValueError("Código de juego no válido")
    Image.MAX_IMAGE_PIXELS = 40_000_000          # evita "bombas de descompresión"
    dest = os.path.join(library, "ART", f"{gid}_COV.png")
    if os.path.exists(dest) and not overwrite:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    try:
        with Image.open(image_path) as im:
            im.convert("RGB").resize((140, 200), Image.LANCZOS).save(tmp, "PNG")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dest


def _fetch_png(url):
    try:
        if not url.startswith("https://"):
            return None
        req = urllib.request.Request(url, headers={"User-Agent": "opl-manager-linux"})
        with urllib.request.urlopen(req, timeout=20) as r:  # nosec B310 - URL https fija (ART_BASE)
            data = r.read(MAX_ART_BYTES + 1)
        if len(data) > MAX_ART_BYTES:
            return None
        return data if data[:8] == b"\x89PNG\r\n\x1a\n" else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def download_art(library, gid, types=None, console="PS2"):
    """Descarga el arte que falta. Nunca sobrescribe. Devuelve la lista de tipos descargados."""
    if not valid_gid(gid) or console not in ("PS2", "PS1"):
        return []
    art = os.path.join(library, "ART")
    os.makedirs(art, exist_ok=True)
    got = []
    for t in (types or ART_TYPES):
        if t not in ART_TYPES:
            continue
        dest = os.path.join(art, f"{gid}_{t}.png")
        if os.path.exists(dest):
            continue
        for remote in ART_TYPES[t]:
            data = _fetch_png(ART_BASE.format(console=console, id=gid, t=remote))
            if data:
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    f.write(data)
                if not os.path.exists(dest):
                    os.replace(tmp, dest)
                else:
                    os.remove(tmp)
                got.append(t)
                break
    return got


OPL_MARKERS = ("DVD", "CD", "ART", "CFG", "CHT", "POPS", "VMC", "THM", "APPS", "OPNPS2LD.ELF")


def _mounts():
    """(punto de montaje, tipo de sistema de archivos) de /proc/mounts."""
    out = []
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = re.sub(r"\\(\d{3})", lambda m: chr(int(m[1], 8)), parts[1])
                out.append((mp, parts[2]))
    except OSError:
        pass
    return out


def fs_type(path):
    """Tipo de sistema de archivos donde está path (exfat, vfat, ntfs3, ext4...)."""
    best, typ = "", ""
    real = os.path.realpath(path)
    for mp, t in _mounts():
        if (real == mp or real.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
            best, typ = mp, t
    return typ


def find_opl_drives():
    """Pendrives y discos montados (en /run/media, /media, /mnt) con lo que tienen de OPL.
    Primero los que tienen estructura OPL, luego los demás (se podrían preparar)."""
    drives = []
    for mp, typ in _mounts():
        if not re.match(r"^/(run/media|media|mnt)/", mp) or not os.path.isdir(mp) or typ in ("udf", "iso9660"):
            continue
        try:
            names = {n.upper() for n in os.listdir(mp)}
        except OSError:
            continue
        markers = [m for m in OPL_MARKERS if m in names]
        games = 0
        for sub in ("DVD", "CD"):
            real = next((n for n in os.listdir(mp) if n.upper() == sub), None)
            if real:
                try:
                    games += sum(1 for n in os.listdir(os.path.join(mp, real)) if n.lower().endswith(".iso"))
                except OSError:
                    pass
        try:
            du = shutil.disk_usage(mp)
            free, total = du.free, du.total
        except OSError:
            free = total = 0
        drives.append({
            "path": mp, "label": os.path.basename(mp), "fs": typ, "free": free, "total": total,
            "markers": markers, "games": games,
            "is_opl": "DVD" in markers or "OPNPS2LD.ELF" in markers or len(markers) >= 3,
        })
    drives.sort(key=lambda d: (not d["is_opl"], -d["games"], d["label"].lower()))
    return drives


def disk_free(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


# ---------------------------------------------------------------- bandeja de entrada
@dataclass
class InboxItem:
    files: list                  # archivos ROM del juego (1 para .iso; varios para .bin/.cue)
    console: str                 # 'PS2' | 'PS1'
    gid: str
    title: str                   # título propuesto (editable)
    size: int
    origin: str                  # carpeta de origen (relativa a la bandeja)
    cover: str = ""              # portada local, si la hay
    busy: bool = False           # se está extrayendo: no se puede importar
    multi: bool = False          # .bin/.cue: no se renombra
    in_library: str = ""         # archivo con el mismo código ya en la biblioteca
    guessed: bool = False        # consola adivinada por tamaño
    extra: dict = field(default_factory=dict)

    @property
    def main(self):
        return self.files[0]


def _recent(path):
    limit = time.time() - RECENT_SECONDS
    for dp, _dn, fn in os.walk(path):
        for n in fn:
            try:
                if os.path.getmtime(os.path.join(dp, n)) > limit:
                    return True
            except OSError:
                pass
    return False


def _classify(files):
    """Consola de un grupo de archivos (lee el disco; si no puede, por tamaño)."""
    biggest, found, gid = 0, "", ""
    for f in files:
        if ext_of(f) not in DATA_EXT:
            continue
        console, g = disc_info(f)
        if console == "PS2":
            return "PS2", g, False
        if console == "PS1":
            found, gid = "PS1", gid or g
        try:
            biggest = max(biggest, os.path.getsize(f))
        except OSError:
            pass
    if found:
        return found, gid, False
    return ("PS2" if biggest > 1_000_000_000 else "PS1"), "", True


_TRACK_RE = re.compile(r"\s*\((track|pista)\s*\d+\)\s*$", re.I)


def group_multi(files):
    """Separa archivos de varios-archivos (.cue/.bin, .mds/.mdf, .ccd/.img/.sub, .m3u) en juegos."""
    files = sorted(files, key=str.lower)
    left = list(files)
    groups = []
    by_name = {os.path.basename(f).lower(): f for f in files}
    for f in files:
        e = ext_of(f)
        if f not in left or e not in ("cue", "m3u", "mds", "ccd"):
            continue
        grp = [f]
        stem = os.path.splitext(os.path.basename(f))[0].lower()
        if e in ("cue", "m3u"):
            try:
                with open(f, encoding="latin-1") as fh:
                    txt = fh.read(65536)
            except OSError:
                txt = ""
            refs = re.findall(r'FILE\s+"([^"]+)"', txt, re.I) if e == "cue" else \
                [ln.strip() for ln in txt.splitlines() if ln.strip() and not ln.startswith("#")]
            for r in refs:
                hit = by_name.get(os.path.basename(r.replace("\\", "/")).lower())
                if hit and hit in left and hit not in grp:
                    grp.append(hit)
        for g in files:   # mismo nombre base (Juego.mdf, Juego.img, Juego.sub, Juego (Track 2).bin...)
            gs = _TRACK_RE.sub("", os.path.splitext(os.path.basename(g))[0]).lower()
            if g in left and g not in grp and gs == stem:
                grp.append(g)
        for g in grp:
            if g in left:
                left.remove(g)
        groups.append(grp)
    rest = {}
    for f in left:          # .bin/.img sueltos sin .cue: por nombre base
        rest.setdefault(_TRACK_RE.sub("", os.path.splitext(os.path.basename(f))[0]).lower(), []).append(f)
    groups += list(rest.values())
    return groups


def scan_inboxes(roots, library="", ps1_dir="CD"):
    """Revisa varias carpetas de ROMs. Devuelve (items, carpetas_que_no_existen)."""
    items, missing, seen = [], [], set()
    for root in roots:
        if not os.path.isdir(root):
            missing.append(root)
            continue
        for it in scan_inbox(root, library, ps1_dir):
            key = os.path.realpath(it.main)
            if key in seen:
                continue
            seen.add(key)
            label = os.path.basename(root.rstrip("/")) or root
            it.origin = label if it.origin in (".", "") else f"{label}/{it.origin}"
            items.append(it)
    return items, missing


def scan_inbox(root, library="", ps1_dir="CD"):
    lib_ids = {}
    if library and os.path.isdir(library):
        for g in scan_library(library):
            if g.gid:
                lib_ids.setdefault(g.gid, g.filename)

    items = []

    def add_group(folder, rom_files, title_hint, busy):
        covers = [os.path.join(dp, n) for dp, _d, fn in os.walk(folder) for n in fn
                  if COVER_RE.search(n) and ext_of(n) in IMG_EXT] if folder != root else []
        solos = [f for f in rom_files if ext_of(f) in SOLO_EXT]
        multi = [f for f in rom_files if ext_of(f) not in SOLO_EXT]
        rel = os.path.relpath(folder, root)
        game_folder = folder if (folder != root and os.path.basename(folder) not in ("DVD", "PS1", "CD", "POPS")) else ""
        base_extra = {"root": root, "folder": folder, "game_folder": game_folder}
        for f in solos:
            console, gid, guessed = _classify([f])
            stem = strip_id(os.path.splitext(os.path.basename(f))[0])
            title = title_hint if (title_hint and len(rom_files) == 1) else stem
            items.append(InboxItem([f], console, gid or id_from_name(os.path.basename(f)), title,
                                   os.path.getsize(f), rel, covers[0] if covers else "", busy,
                                   False, guessed=guessed, extra=dict(base_extra)))
        for grp in (group_multi(multi) if multi else []):
            # .cue primero, luego los .bin
            grp.sort(key=lambda p: (ext_of(p) not in ("cue", "m3u", "mds", "ccd"), p.lower()))
            console, gid, guessed = _classify(grp)
            stem = os.path.splitext(os.path.basename(grp[0]))[0]
            items.append(InboxItem(grp, console, gid, stem, sum(os.path.getsize(x) for x in grp),
                                   rel, covers[0] if covers else "", busy, True, guessed=guessed,
                                   extra=dict(base_extra)))

    if not os.path.isdir(root):
        return items
    entries = sorted(os.listdir(root), key=str.lower)
    # sueltos en la raíz
    loose = [os.path.join(root, n) for n in entries
             if os.path.isfile(os.path.join(root, n)) and ext_of(n) in ROM_EXT]
    if loose:
        add_group(root, loose, "", False)
    for n in entries:
        d = os.path.join(root, n)
        if not os.path.isdir(d) or n in ("ART", "pnach"):
            continue
        roms = sorted((os.path.join(dp, x) for dp, _dn, fn in os.walk(d) for x in fn if ext_of(x) in ROM_EXT),
                      key=str.lower)
        if not roms:
            continue
        hint = "" if n in ("DVD", "PS1", "CD", "POPS") else n
        if n in ("DVD", "PS1", "CD"):
            # carpetas ya ordenadas: cada archivo/juego por separado
            by_stem = {}
            for r in roms:
                key = os.path.splitext(os.path.basename(r))[0].split(" (Track")[0]
                by_stem.setdefault(key if ext_of(r) not in SOLO_EXT else r, []).append(r)
            for group in by_stem.values():
                add_group(d, group, "", _recent(d) and any(time.time() - os.path.getmtime(g) < RECENT_SECONDS for g in group))
        else:
            add_group(d, roms, hint, _recent(d))

    for it in items:
        if it.gid and it.gid in lib_ids:
            it.in_library = lib_ids[it.gid]
        elif it.multi and library:
            hit = [os.path.basename(f) for f in it.files
                   if os.path.exists(os.path.join(library, ps1_dir or "CD", os.path.basename(f)))]
            if hit:
                it.in_library = hit[0]
        if it.console == "PS1":
            it.extra["vcd_ok"] = pops.can_convert(it.files)
            if it.extra["vcd_ok"] and it.multi:
                # el VCD es un solo archivo: el título se puede editar como en los de PS2
                cue = next((f for f in it.files if ext_of(f) == "cue"), it.main)
                it.title = strip_id(os.path.splitext(os.path.basename(cue))[0])
        # arte que ya está en la bandeja (p. ej. bajado con opl-art.py)
        if it.gid:
            it.extra["inbox_art"] = {t: p for t in ART_TYPES
                                     if os.path.exists(p := os.path.join(root, "ART", f"{it.gid}_{t}.png"))}
    return items


def cleanup_targets(items):
    """Qué mandar a la papelera para quitar estos juegos de la bandeja.
    Si un juego está en su propia carpeta (p. ej. "Killer7 [PAL]/") y en ella no queda ningún otro
    juego, se elimina la carpeta entera (con sus .url, .txt, portadas...). Si no, solo sus archivos.
    Devuelve una lista de rutas (carpetas o archivos), sin repetidos ni rutas anidadas."""
    files = {os.path.realpath(f) for it in items for f in it.files}
    by_folder = {}
    for it in items:
        gf = it.extra.get("game_folder", "")
        by_folder.setdefault(gf, []).append(it)
    targets = []
    for gf, its in by_folder.items():
        if gf and os.path.isdir(gf):
            roms_in_folder = {os.path.realpath(os.path.join(dp, n))
                              for dp, _d, fn in os.walk(gf) for n in fn if ext_of(n) in ROM_EXT}
            if roms_in_folder <= files:          # no queda ningún otro juego dentro
                targets.append(gf)
                continue
        for it in its:
            targets += [f for f in it.files if os.path.exists(f)]
    # sin rutas dentro de otra ya incluida
    targets = sorted(set(targets), key=len)
    out = []
    for t in targets:
        if not any(t.startswith(o.rstrip("/") + "/") for o in out):
            out.append(t)
    return out


def game_art_files(game, all_games):
    """Imágenes de ART/ de este juego, solo si ningún otro juego de la biblioteca usa el mismo código."""
    if not game.gid or any(g is not game and g.gid == game.gid for g in all_games):
        return []
    return [p for t in ART_TYPES if (p := game.art(t))]


def path_size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    return sum(os.path.getsize(os.path.join(dp, n)) for dp, _d, fn in os.walk(path) for n in fn
               if os.path.isfile(os.path.join(dp, n)))


def _copy_file(src, dst, progress=None, done_before=0, total=0):
    """Copia por bloques a dst.part y renombra al final. Nunca sobrescribe."""
    if os.path.exists(dst):
        raise FileExistsError(f"Ya existe: {os.path.basename(dst)}")
    tmp = dst + ".part"
    copied = 0
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                chunk = fi.read(8 * 1024 * 1024)
                if not chunk:
                    break
                fo.write(chunk)
                copied += len(chunk)
                if progress:
                    progress(done_before + copied, total)
            fo.flush()
            os.fsync(fo.fileno())
        if os.path.getsize(tmp) != os.path.getsize(src):
            raise OSError("La copia quedó incompleta")
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return copied


def _is_plain_png(path):
    """Archivo PNG normal (no enlace simbólico) y de tamaño razonable."""
    try:
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > MAX_ART_BYTES:
            return False
        with open(path, "rb") as f:
            return f.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def import_item(item, cfg, progress=None):
    """Copia un juego de la bandeja a la biblioteca (sin borrar nada en ningún lado). Los de PS1 se
    convierten a .VCD para POPS si así está configurado. Devuelve (lista de rutas copiadas, lista de arte agregado)."""
    lib = cfg["library"]
    if item.in_library:
        raise FileExistsError(f"Ya está en la biblioteca como {item.in_library}")
    action, folder, name = item_destination(item, cfg)
    dest_dir = os.path.join(lib, folder)
    os.makedirs(dest_dir, exist_ok=True)

    if action == "vcd":
        target = free_path(os.path.join(dest_dir, name))
        need = pops.vcd_size(pops.plan_for(item.files))
        if disk_free(lib) < need + 50 * 1024 * 1024:
            raise OSError(f"No hay espacio: hacen falta {human_size(need)} y quedan {human_size(disk_free(lib))}")
        pops.convert(item.files, target, progress)
        copied = [target]
    else:
        copied = _copy_item(item, dest_dir, name, lib, progress)
    art = _import_art(item, cfg, lib)
    if action == "vcd" and cfg.get("pops_apps", True):
        try:
            ensure_pops_files(lib)
            make_pops_app(lib, target)
            link_pops_app_art(Game(target, "POPS", item.gid if valid_gid(item.gid) else "", "", 0, lib))
        except (OSError, ValueError) as e:
            item.extra["pops_app_error"] = str(e)
    return copied, art


def _copy_item(item, dest_dir, name, lib, progress):
    if item.multi:
        targets = [os.path.join(dest_dir, os.path.basename(f)) for f in item.files]
        clash = [t for t in targets if os.path.exists(t)]
        if clash:
            raise FileExistsError(f"Ya existe en la biblioteca: {os.path.basename(clash[0])}")
    else:
        targets = [free_path(os.path.join(dest_dir, name))]

    total = sum(os.path.getsize(f) for f in item.files)
    if fs_type(lib) in ("vfat", "msdos", "fat") and any(os.path.getsize(f) >= 4 * 1024 ** 3 for f in item.files):
        raise OSError("El pendrive es FAT32 y no admite archivos de 4 GB o más. Formatéalo en exFAT "
                      "(OPL 1.1+) o divide la ISO con USBUtil.")
    if disk_free(lib) < total + 50 * 1024 * 1024:
        raise OSError(f"No hay espacio: hacen falta {human_size(total)} y quedan {human_size(disk_free(lib))}")

    done = 0
    copied = []
    for src, dst in zip(item.files, targets):
        done += _copy_file(src, dst, progress, done, total)
        copied.append(dst)
    return copied


def _import_art(item, cfg, lib):
    """Arte del juego recién copiado: el de la bandeja, la portada local y el de la base (PS2 o PS1)."""
    art = []
    if item.gid:
        art_dir = os.path.join(lib, "ART")
        os.makedirs(art_dir, exist_ok=True)
        for t, p in item.extra.get("inbox_art", {}).items():
            dest = os.path.join(art_dir, f"{item.gid}_{t}.png")
            if t in ART_TYPES and valid_gid(item.gid) and not os.path.exists(dest) and _is_plain_png(p):
                shutil.copyfile(p, dest)
                art.append(t)
        if item.cover and not os.path.exists(os.path.join(art_dir, f"{item.gid}_COV.png")):
            try:
                save_cover(lib, item.gid, item.cover)
                art.append("COV")
            except Exception:
                pass
        if cfg.get("download_art", True):
            art += download_art(lib, item.gid, console="PS1" if item.console == "PS1" else "PS2")
    return art


def ps1_dir(cfg):
    """Carpeta de la biblioteca para los .bin/.cue copiados tal cual (siempre dentro de la biblioteca)."""
    d = re.sub(r"[^A-Za-z0-9 _.-]", "", str(cfg.get("ps1_dir") or "CD")).strip(" .") or "CD"
    return d


def item_destination(item, cfg):
    """Qué se hará con un juego de la bandeja: (acción, carpeta, nombre).
    acción: 'copy' (copiar tal cual) o 'vcd' (convertir a VCD para POPS). nombre: '' si se copian
    varios archivos con su nombre original."""
    max_len = int(cfg.get("max_name", 32) or 0)
    if item.console == "PS2" and not item.multi:
        return "copy", "DVD", opl_filename(item.gid, item.title, ext_of(item.main), max_len)
    if item.console == "PS1" and cfg.get("ps1_mode", "vcd") == "vcd" and item.extra.get("vcd_ok"):
        return "vcd", "POPS", opl_filename(item.gid, item.title, "VCD", max_len)
    if item.multi:
        return "copy", ps1_dir(cfg), ""
    return "copy", ps1_dir(cfg), opl_filename(item.gid, item.title, ext_of(item.main), max_len)


# ---------------------------------------------------------------- POPStarter: archivos y accesos en APPS/
# POPSTARTER.ELF y POPS_IOX.PAK no se distribuyen con la app (POPS_IOX.PAK es de Sony). Se guarda una copia
# local la primera vez que aparecen en una unidad (o que el usuario los elige) y se ponen en las unidades
# donde falten.
POPS_STORE = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share"),
                          "opl-manager", "pops")
POPS_FILES = ("POPSTARTER.ELF", "POPS_IOX.PAK")
_POPS_MAX = 16 * 1024 * 1024


def _find_ci(folder, name):
    """Ruta de folder/name sin importar mayúsculas (FAT/exFAT), o ''."""
    try:
        for n in os.listdir(folder):
            if n.upper() == name.upper():
                return os.path.join(folder, n)
    except OSError:
        pass
    return ""


def _valid_pops_file(name, path):
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return False
        size = os.path.getsize(path)
        if not 1024 <= size <= _POPS_MAX:
            return False
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return head == b"\x7fELF" if name == "POPSTARTER.ELF" else True


def _copy_atomic(src, dst):
    tmp = dst + ".part"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def pops_store_file(name):
    p = os.path.join(POPS_STORE, name)
    return p if _valid_pops_file(name, p) else ""


def learn_pops_files(library):
    """Guarda en el equipo los archivos de POPS que tenga la unidad y falten en la copia local."""
    learned = []
    for name in POPS_FILES:
        src = _find_ci(os.path.join(library, "POPS"), name)
        if not pops_store_file(name) and src and _valid_pops_file(name, src):
            os.makedirs(POPS_STORE, exist_ok=True)
            _copy_atomic(src, os.path.join(POPS_STORE, name))
            learned.append(name)
    return learned


def import_pops_file(path):
    """El usuario eligió un archivo: se guarda como POPSTARTER.ELF o POPS_IOX.PAK. Devuelve cuál fue."""
    base = os.path.basename(path).upper()
    if base == "POPS_IOX.PAK":
        name = "POPS_IOX.PAK"
    elif _valid_pops_file("POPSTARTER.ELF", path):
        name = "POPSTARTER.ELF"
    else:
        raise ValueError("No es POPSTARTER.ELF (un ejecutable ELF) ni POPS_IOX.PAK")
    if not _valid_pops_file(name, path):
        raise ValueError(f"{os.path.basename(path)} no parece un {name} válido")
    os.makedirs(POPS_STORE, exist_ok=True)
    _copy_atomic(path, os.path.join(POPS_STORE, name))
    return name


def ensure_pops_files(library):
    """Pone en POPS/ de la unidad los archivos que falten, desde la copia local.
    Devuelve (copiados, todavía faltantes)."""
    learn_pops_files(library)
    d = os.path.join(library, "POPS")
    copied, missing = [], []
    for name in POPS_FILES:
        if _find_ci(d, name):
            continue
        src = pops_store_file(name)
        if not src:
            missing.append(name)
            continue
        os.makedirs(d, exist_ok=True)
        _copy_atomic(src, os.path.join(d, name))
        copied.append(name)
    return copied, missing


def pops_app_dir(library, vcd_path):
    return os.path.join(library, "APPS", os.path.splitext(os.path.basename(vcd_path))[0])


def _pops_app_files(stem):
    return f"XX.{stem}.ELF", "title.cfg"


def _app_title(stem):
    return clean_title(strip_id(stem)) or stem


def has_pops_app(library, vcd_path):
    d = pops_app_dir(library, vcd_path)
    elf, cfg = _pops_app_files(os.path.basename(d))
    return bool(_find_ci(d, elf)) and bool(_find_ci(d, cfg))


def _write_title_cfg(path, title, boot):
    title = re.sub(r"[\r\n=]", " ", title).strip()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"title={title}\nboot={boot}\n")


def make_pops_app(library, vcd_path):
    """Acceso para que OPL muestre el juego de PS1 en Aplicaciones:
    APPS/<nombre>/XX.<nombre>.ELF (copia de POPSTARTER.ELF) y APPS/<nombre>/title.cfg.
    POPStarter busca POPS/<nombre>.VCD a partir del nombre del ELF. No pisa nada que ya exista.
    Devuelve la carpeta creada, o '' si ya estaba."""
    if has_pops_app(library, vcd_path):
        return ""
    stem = os.path.splitext(os.path.basename(vcd_path))[0]
    if not stem or stem.startswith(".") or os.sep in stem:
        raise ValueError(f"Nombre de juego no válido: {stem!r}")
    elf_src = _find_ci(os.path.join(library, "POPS"), "POPSTARTER.ELF")
    if not (elf_src and _valid_pops_file("POPSTARTER.ELF", elf_src)):
        elf_src = pops_store_file("POPSTARTER.ELF")
    if not elf_src:
        raise FileNotFoundError("Falta POPSTARTER.ELF (agrégalo en Ajustes)")
    d = pops_app_dir(library, vcd_path)
    os.makedirs(d, exist_ok=True)
    elf, cfg = _pops_app_files(stem)
    if not _find_ci(d, elf):
        _copy_atomic(elf_src, os.path.join(d, elf))
    if not _find_ci(d, cfg):
        _write_title_cfg(os.path.join(d, cfg), _app_title(stem), elf)
    return d


def pops_app_art_name(vcd_path, t):
    """OPL busca el arte de una app por el nombre de su ELF: ART/XX.<nombre>.ELF_COV.png"""
    stem = os.path.splitext(os.path.basename(vcd_path))[0]
    return f"{_pops_app_files(stem)[0]}_{t}.png"


def pops_app_art_files(library, vcd_path):
    art = os.path.join(library, "ART")
    return [p for t in ART_TYPES if os.path.isfile(p := os.path.join(art, pops_app_art_name(vcd_path, t)))]


def link_pops_app_art(game):
    """Copia el arte del juego (ART/<código>_COV.png...) con el nombre que OPL usa para su app.
    Copia y no enlace: exFAT/FAT32 no tienen enlaces simbólicos. Nunca pisa nada. Devuelve cuántas copió."""
    if game.kind != "POPS" or not game.gid or not has_pops_app(game.library, game.path):
        return 0
    n = 0
    for t in ART_TYPES:
        src = game.art(t)
        dst = os.path.join(game.library, "ART", pops_app_art_name(game.path, t))
        if src and not os.path.exists(dst) and _is_plain_png(src):
            _copy_atomic(src, dst)
            n += 1
    return n


def sync_pops_app_art(games):
    n = 0
    for g in games:
        try:
            n += link_pops_app_art(g)
        except OSError:
            pass
    return n


def missing_pops_apps(games):
    return [g for g in games if g.kind == "POPS" and not has_pops_app(g.library, g.path)]


def _rename_pops_app(library, old_vcd, new_vcd):
    """El acceso de APPS/ sigue al .VCD cuando se renombra (si no, POPStarter no lo encuentra)."""
    old_d, new_d = pops_app_dir(library, old_vcd), pops_app_dir(library, new_vcd)
    if not os.path.isdir(old_d) or os.path.islink(old_d):
        return
    old_stem, new_stem = os.path.basename(old_d), os.path.basename(new_d)
    old_elf = _find_ci(old_d, _pops_app_files(old_stem)[0])
    if not old_elf:
        return          # carpeta que no es nuestra: no se toca
    _move(old_d, new_d)
    new_elf = _pops_app_files(new_stem)[0]
    _move(os.path.join(new_d, os.path.basename(old_elf)), os.path.join(new_d, new_elf))
    cfg = _find_ci(new_d, "title.cfg")
    if cfg:
        _write_title_cfg(cfg, _app_title(new_stem), new_elf)
    for t in ART_TYPES:                   # el arte de la app va por el nombre del ELF
        old_art = os.path.join(library, "ART", pops_app_art_name(old_vcd, t))
        new_art = os.path.join(library, "ART", pops_app_art_name(new_vcd, t))
        if os.path.isfile(old_art) and not os.path.exists(new_art):
            _move(old_art, new_art)


def pops_ready(library):
    """POPStarter necesita POPS/POPS_IOX.PAK en la unidad para arrancar los .VCD."""
    d = os.path.join(library, "POPS")
    try:
        return any(n.upper() == "POPS_IOX.PAK" for n in os.listdir(d))
    except OSError:
        return False
