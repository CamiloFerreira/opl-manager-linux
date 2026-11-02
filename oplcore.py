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

__version__ = "0.1.0"

# ---------------------------------------------------------------- constantes
ID_RE = re.compile(r"([A-Z]{4})[_-](\d{3})\.(\d{2})")
ID_PREFIX_RE = re.compile(r"^([A-Z]{4})[_-](\d{3})\.(\d{2})[._ -]")

ROM_EXT = {"iso", "bin", "cue", "img", "chd", "cso", "zso", "mdf", "mds", "nrg", "ecm", "pbp", "ccd", "sub", "m3u"}
SOLO_EXT = {"iso", "chd", "cso", "zso", "pbp", "nrg"}           # un solo archivo
DATA_EXT = {"iso", "bin", "img", "mdf", "nrg", "chd", "cso", "zso", "pbp", "ecm"}
COVER_RE = re.compile(r"(cover|portada|car[aá]tula)", re.I)
IMG_EXT = {"png", "jpg", "jpeg", "webp"}

ART_BASE = "https://raw.githubusercontent.com/Luden02/psx-ps2-opl-art-database/main/PS2/{id}/{id}_{t}.png"
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
    "ps1_dir": "CD",            # carpeta de la biblioteca donde van los .bin/.cue de PS1
    "max_name": 32,             # 0 = sin límite (OPL nuevo)
    "download_art": True,
    "sort": 0,                  # 0 nombre, 1 código, 2 tamaño, 3 más recientes
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


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
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
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(ID_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache(), f)
    except OSError:
        pass


def _read_boot_line(path):
    """Línea BOOT/BOOT2 del SYSTEM.CNF, buscando el archivo en el índice ISO9660
    (2048 o 2352 bytes por sector). Si falla, busca el texto en los primeros 8 MB."""
    try:
        f = open(path, "rb")
    except OSError:
        return ""
    with f:
        def sec(lba, size, off):
            f.seek(lba * size + off)
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
                    name = data[i + 33:i + 33 + data[i + 32]].split(b";")[0].upper()
                    if name == b"SYSTEM.CNF":
                        elba = int.from_bytes(data[i + 2:i + 6], "little")
                        txt = sec(elba, size, off).decode("latin-1")
                        m = re.search(r"BOOT2? *= *cdrom0?:[^\r\n;]*", txt)
                        if m:
                            return m.group(0)
                    i += n
                break
            f.seek(0)
            m = re.search(rb"BOOT2? *= *cdrom0?:[^\r\n;]{0,40}", f.read(8_000_000))
            return m.group(0).decode("latin-1") if m else ""
        except (OSError, ValueError):
            return ""


def disc_info(path):
    """(consola, código) del disco: ('PS2'|'PS1'|'', 'SLUS_209.46'|''). Con caché."""
    try:
        st = os.stat(path)
    except OSError:
        return "", ""
    key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
    c = _cache()
    if key in c:
        return tuple(c[key])
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
    if not max_len or len(t) <= max_len:
        return t.rstrip(" .-_,") or title
    while len(t) > max_len:
        new = _TAG_END.sub("", t)
        new = _TAG_START.sub("", new)
        if new == t or not new:
            break
        t = new
    if len(t) > max_len:
        cut = t[:max_len + 1]
        t = cut.rsplit(" ", 1)[0] if " " in cut else t[:max_len]
    return t.rstrip(" .-_,") or title[:max_len]


def opl_filename(gid, title, ext, max_len=32):
    t = short_title(title, max_len)
    return f"{gid}.{t}.{ext}" if gid else f"{t}.{ext}"


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
            if not gid and kind != "POPS":
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


def rename_game(game, new_title, max_len=32):
    """Renombra el archivo del juego (no borra nada). Devuelve la nueva ruta."""
    ext = ext_of(game.path)
    new = os.path.join(os.path.dirname(game.path), opl_filename(game.gid, new_title, ext, max_len))
    if new == game.path:
        return new
    if os.path.exists(new):
        raise FileExistsError(f"Ya existe: {os.path.basename(new)}")
    os.rename(game.path, new)
    return new


def save_cover(library, gid, image_path, overwrite=False):
    """Guarda una imagen como ART/<código>_COV.png (140x200)."""
    from PIL import Image
    dest = os.path.join(library, "ART", f"{gid}_COV.png")
    if os.path.exists(dest) and not overwrite:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    Image.open(image_path).convert("RGB").resize((140, 200), Image.LANCZOS).save(tmp, "PNG")
    os.replace(tmp, dest)
    return dest


def _fetch_png(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "opl-manager-linux"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        return data if data[:8] == b"\x89PNG\r\n\x1a\n" else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def download_art(library, gid, types=None):
    """Descarga el arte que falta. Nunca sobrescribe. Devuelve la lista de tipos descargados."""
    if not gid:
        return []
    art = os.path.join(library, "ART")
    os.makedirs(art, exist_ok=True)
    got = []
    for t in (types or ART_TYPES):
        dest = os.path.join(art, f"{gid}_{t}.png")
        if os.path.exists(dest):
            continue
        for remote in ART_TYPES[t]:
            data = _fetch_png(ART_BASE.format(id=gid, t=remote))
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
        for f in solos:
            console, gid, guessed = _classify([f])
            stem = strip_id(os.path.splitext(os.path.basename(f))[0])
            title = title_hint if (title_hint and len(rom_files) == 1) else stem
            items.append(InboxItem([f], console, gid or id_from_name(os.path.basename(f)), title,
                                   os.path.getsize(f), rel, covers[0] if covers else "", busy,
                                   False, guessed=guessed))
        for grp in (group_multi(multi) if multi else []):
            # .cue primero, luego los .bin
            grp.sort(key=lambda p: (ext_of(p) not in ("cue", "m3u", "mds", "ccd"), p.lower()))
            console, gid, guessed = _classify(grp)
            stem = os.path.splitext(os.path.basename(grp[0]))[0]
            items.append(InboxItem(grp, console, gid, stem, sum(os.path.getsize(x) for x in grp),
                                   rel, covers[0] if covers else "", busy, True, guessed=guessed))

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
        if it.gid and it.gid in lib_ids and not it.multi:
            it.in_library = lib_ids[it.gid]
        elif it.multi and library:
            hit = [os.path.basename(f) for f in it.files
                   if os.path.exists(os.path.join(library, ps1_dir or "CD", os.path.basename(f)))]
            if hit:
                it.in_library = hit[0]
        # arte que ya está en la bandeja (p. ej. bajado con opl-art.py)
        if it.gid:
            it.extra["inbox_art"] = {t: p for t in ART_TYPES
                                     if os.path.exists(p := os.path.join(root, "ART", f"{it.gid}_{t}.png"))}
    return items


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


def import_item(item, cfg, progress=None):
    """Copia un juego de la bandeja a la biblioteca (sin borrar nada en ningún lado).
    Devuelve (lista de rutas copiadas, lista de arte agregado)."""
    lib = cfg["library"]
    max_len = int(cfg.get("max_name", 32) or 0)
    if item.in_library:
        raise FileExistsError(f"Ya está en la biblioteca como {item.in_library}")
    if item.console == "PS2" and not item.multi:
        dest_dir = os.path.join(lib, "DVD")
    else:
        dest_dir = os.path.join(lib, cfg.get("ps1_dir") or "CD")
    os.makedirs(dest_dir, exist_ok=True)

    if item.multi:
        targets = [os.path.join(dest_dir, os.path.basename(f)) for f in item.files]
        clash = [t for t in targets if os.path.exists(t)]
        if clash:
            raise FileExistsError(f"Ya existe en la biblioteca: {os.path.basename(clash[0])}")
    else:
        name = opl_filename(item.gid, item.title, ext_of(item.main), max_len)
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

    art = []
    if item.gid:
        art_dir = os.path.join(lib, "ART")
        os.makedirs(art_dir, exist_ok=True)
        for t, p in item.extra.get("inbox_art", {}).items():
            dest = os.path.join(art_dir, f"{item.gid}_{t}.png")
            if not os.path.exists(dest):
                shutil.copyfile(p, dest)
                art.append(t)
        if item.cover and not os.path.exists(os.path.join(art_dir, f"{item.gid}_COV.png")):
            try:
                save_cover(lib, item.gid, item.cover)
                art.append("COV")
            except Exception:
                pass
        if cfg.get("download_art", True):
            art += download_art(lib, item.gid)
    return copied, art
