"""
Conversión de juegos de PS1 (.cue/.bin) a imagen .VCD para POPStarter (POPS).

Formato compatible con cue2pops v2.0 (krHACKen):
  - Cabecera de 1 MiB (0x100000) con la tabla de contenido en BCD:
      0..9    descriptor A0 (tipo de disco: CD-XA, primera pista 1)
      10..19  descriptor A1 (tipo de la última pista y número de la última pista)
      20..29  descriptor A2 (lead-out)
      30..    una entrada de 10 bytes por pista: tipo, número, INDEX 00 y INDEX 01 (MSF absolutos)
      1024    firma "kHn " de cue2pops
      1032    número de sectores (32 bits, little-endian), repetido en 1036
  - Luego, los sectores crudos de 2352 bytes del disco.

Además de lo que admite cue2pops, se aceptan dumps con una pista por archivo (Redump): los .bin se unen
en orden al escribir el VCD, sin archivos intermedios.
"""
import os
import re

SECTOR = 2352
HEADER = 0x100000
LEADIN = 150                  # 2 segundos: los MSF absolutos empiezan en 00:02:00
SYNC = b"\x00" + b"\xff" * 10 + b"\x00"
CHUNK = 8 * 1024 * 1024


class CueError(ValueError):
    """La imagen no se puede convertir (formato no admitido o cue dañado)."""


def _bcd(n):
    return (n // 10) * 16 + n % 10


def _msf(lba):
    """Sector -> (MM, SS, FF) en BCD."""
    m, rest = divmod(lba, 4500)
    s, f = divmod(rest, 75)
    if m > 99:
        raise CueError("El disco es demasiado grande")
    return _bcd(m), _bcd(s), _bcd(f)


def _frames(text):
    m = re.fullmatch(r"(\d{1,3}):(\d{2}):(\d{2})", text)
    if not m or int(m[2]) > 59 or int(m[3]) > 74:
        raise CueError(f"Tiempo no válido en el cue: {text}")
    return int(m[1]) * 4500 + int(m[2]) * 75 + int(m[3])


def _find(folder, name):
    """Archivo referenciado por el cue, solo dentro de la carpeta del cue (sin rutas). Ignora mayúsculas."""
    base = os.path.basename(name.replace("\\", "/"))
    p = os.path.join(folder, base)
    if os.path.isfile(p):
        return p
    low = base.lower()
    for n in os.listdir(folder):
        if n.lower() == low and os.path.isfile(os.path.join(folder, n)):
            return os.path.join(folder, n)
    raise CueError(f"Falta el archivo {base}")


def parse_cue(cue_path):
    """Lee el cue y devuelve el plan del disco:
    {'files': [(ruta, sectores)], 'tracks': [{'num', 'audio', 'idx0', 'idx1'}], 'pad_at': sector|None}
    Los índices son sectores absolutos dentro de los .bin unidos (antes de insertar el pregap)."""
    folder = os.path.dirname(os.path.abspath(cue_path))
    try:
        with open(cue_path, encoding="latin-1") as f:
            text = f.read(256 * 1024)
    except OSError as e:
        raise CueError(f"No se pudo leer el cue: {e}") from e

    files, tracks, pregaps, postgaps = [], [], [], 0
    offset = 0                                  # sector donde empieza el archivo actual
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.upper().startswith("REM"):
            continue
        word = line.split(None, 1)[0].upper()
        if word == "FILE":
            m = re.match(r'FILE\s+(?:"([^"]+)"|(\S+))\s+(\S+)', line, re.I)
            if not m:
                raise CueError("Línea FILE no válida en el cue")
            if m[3].upper() != "BINARY":
                raise CueError("El cue usa pistas de audio WAVE/MP3: conviértelas a .bin primero")
            if files:
                offset += files[-1][1]
            path = _find(folder, m[1] or m[2])
            size = os.path.getsize(path)
            if size == 0 or size % SECTOR:
                raise CueError(f"{os.path.basename(path)} no es una imagen cruda de 2352 bytes por sector")
            files.append((path, size // SECTOR))
        elif word == "TRACK":
            m = re.match(r"TRACK\s+(\d{1,2})\s+(\S+)", line, re.I)
            if not m or not files:
                raise CueError("Línea TRACK no válida en el cue")
            mode = m[2].upper()
            if not tracks and mode != "MODE2/2352":
                raise CueError(f"La primera pista es {mode}: POPS necesita MODE2/2352")
            if mode not in ("MODE2/2352", "MODE1/2352", "AUDIO"):
                raise CueError(f"Tipo de pista no admitido: {mode}")
            cur = {"num": int(m[1]), "audio": mode == "AUDIO", "idx0": None, "idx1": None}
            tracks.append(cur)
        elif word == "INDEX":
            m = re.match(r"INDEX\s+(\d{1,2})\s+(\S+)", line, re.I)
            if not m or cur is None:
                raise CueError("Línea INDEX no válida en el cue")
            pos = offset + _frames(m[2])
            if int(m[1]) == 0 and cur["idx1"] is None:
                cur["idx0"] = pos
            elif int(m[1]) == 1 and cur["idx1"] is None:
                cur["idx1"] = pos
        elif word == "PREGAP":
            if cur is None:
                raise CueError("PREGAP fuera de una pista")
            pregaps.append((cur, _frames(line.split()[1]) if len(line.split()) > 1 else 0))
        elif word == "POSTGAP":
            postgaps += 1

    if not files or not tracks:
        raise CueError("El cue no tiene archivos ni pistas")
    if len(tracks) > 99:
        raise CueError("Demasiadas pistas")
    for t in tracks:
        if t["idx1"] is None:
            raise CueError(f"La pista {t['num']} no tiene INDEX 01")
        if t["idx0"] is None:
            t["idx0"] = t["idx1"]
    if tracks[0]["idx1"] >= 4500:
        raise CueError("El cue no es válido (la primera pista no empieza al inicio)")

    # cue de CDRWIN: un único PREGAP (2 s) antes de la primera pista de audio, que no está en el .bin.
    # Igual que cue2pops, se insertan 150 sectores vacíos en ese punto.
    pad_at = None
    if pregaps or postgaps:
        first_audio = next((t for t in tracks if t["audio"]), None)
        if postgaps or len(pregaps) != 1 or pregaps[0][1] != LEADIN or pregaps[0][0] is not first_audio:
            raise CueError("El cue usa PREGAP/POSTGAP de una forma no admitida")
        pad_at = first_audio["idx0"]
    return {"files": files, "tracks": tracks, "pad_at": pad_at}


def plan_single_bin(bin_path):
    """Un .bin suelto sin cue: se asume una sola pista de datos MODE2/2352."""
    size = os.path.getsize(bin_path)
    if size == 0 or size % SECTOR:
        raise CueError("El .bin no es una imagen cruda de 2352 bytes por sector")
    return {"files": [(bin_path, size // SECTOR)],
            "tracks": [{"num": 1, "audio": False, "idx0": 0, "idx1": 0}], "pad_at": None}


def build_header(plan):
    """Cabecera POPS de 1 MiB para el plan del disco."""
    tracks, pad_at = plan["tracks"], plan["pad_at"]
    total = sum(n for _p, n in plan["files"]) + (LEADIN if pad_at is not None else 0)
    h = bytearray(HEADER)
    h[0], h[2], h[7], h[8] = 0x41, 0xA0, 0x01, 0x20          # A0: CD-XA, primera pista 1
    h[12] = 0xA1
    h[22] = 0xA2
    last_type = 0x01 if tracks[-1]["audio"] else 0x41
    h[10] = h[20] = last_type
    h[17] = _bcd(tracks[-1]["num"])
    h[27:30] = bytes(_msf(total + LEADIN))                  # lead-out

    def shifted(lba):
        return lba + (LEADIN if pad_at is not None and lba >= pad_at else 0)

    for i, t in enumerate(tracks):
        e = 30 + 10 * i
        h[e] = 0x01 if t["audio"] else 0x41
        h[e + 2] = _bcd(t["num"])
        idx0 = shifted(t["idx0"]) + (LEADIN if i else 0)    # la pista 1 deja su INDEX 00 en 00:00:00
        h[e + 3:e + 6] = bytes(_msf(idx0))
        h[e + 7:e + 10] = bytes(_msf(shifted(t["idx1"]) + LEADIN))
    h[1024:1028] = b"kHn "
    h[1032:1036] = total.to_bytes(4, "little")
    h[1036:1040] = total.to_bytes(4, "little")
    return bytes(h)


def plan_for(files):
    """Plan de conversión para los archivos de un juego de la bandeja, o CueError si no se puede."""
    cues = [f for f in files if f.lower().endswith(".cue")]
    bins = [f for f in files if f.lower().endswith(".bin")]
    if len(cues) == 1:
        plan = parse_cue(cues[0])
    elif not cues and len(bins) == 1:
        plan = plan_single_bin(bins[0])
    elif len(cues) > 1:
        raise CueError("Juego de varios discos: conviértelo disco por disco")
    else:
        raise CueError("Para convertir a VCD hace falta el .cue y su .bin")
    with open(plan["files"][0][0], "rb") as f:
        if f.read(12) != SYNC:
            raise CueError("La imagen no es un volcado crudo de CD (2352 bytes por sector)")
    return plan


def can_convert(files):
    try:
        plan_for(files)
        return True
    except (CueError, OSError):
        return False


def vcd_size(plan):
    return HEADER + (sum(n for _p, n in plan["files"]) + (LEADIN if plan["pad_at"] is not None else 0)) * SECTOR


def convert(files, dest, progress=None):
    """Escribe el VCD en dest (vía dest.part; nunca sobrescribe). Devuelve los bytes escritos."""
    plan = plan_for(files)
    if os.path.exists(dest):
        raise FileExistsError(f"Ya existe: {os.path.basename(dest)}")
    total = vcd_size(plan)
    pad_byte = plan["pad_at"] * SECTOR if plan["pad_at"] is not None else -1
    tmp = dest + ".part"
    written = 0
    try:
        with open(tmp, "wb") as out:
            out.write(build_header(plan))
            written = HEADER
            pos = 0                                         # posición en los .bin unidos
            for path, _n in plan["files"]:
                with open(path, "rb") as fi:
                    while True:
                        want = CHUNK
                        if pos < pad_byte < pos + want:
                            want = pad_byte - pos           # cortar justo donde va el pregap
                        chunk = fi.read(want)
                        if not chunk:
                            break
                        if pos == pad_byte:
                            out.write(b"\x00" * (LEADIN * SECTOR))
                            written += LEADIN * SECTOR
                        out.write(chunk)
                        pos += len(chunk)
                        written += len(chunk)
                        if progress:
                            progress(written, total)
            if pos == pad_byte:                             # pregap justo al final (no debería pasar)
                out.write(b"\x00" * (LEADIN * SECTOR))
                written += LEADIN * SECTOR
            out.flush()
            os.fsync(out.fileno())
        if os.path.getsize(tmp) != total:
            raise OSError("La conversión quedó incompleta")
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return written
