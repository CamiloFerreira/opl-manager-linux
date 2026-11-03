"""Pruebas de la conversión .cue/.bin -> .VCD (POPS).
Los valores esperados de la cabecera se calcularon a mano siguiendo cue2pops v2.0."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import oplcore as core  # noqa: E402
import pops  # noqa: E402
from test_core import make_iso  # noqa: E402

S = pops.SECTOR


def raw_bin(path, sectors, fill=b"", sync=True):
    """Imagen cruda de 2352 bytes por sector (el primer sector con la marca de sincronismo)."""
    data = bytearray(sectors * S)
    if sync:
        data[0:12] = pops.SYNC
    for i in range(sectors):          # marca cada sector para poder comprobar el orden
        data[i * S + 100:i * S + 104] = i.to_bytes(4, "little")
    data[200:200 + len(fill)] = fill
    with open(path, "wb") as f:
        f.write(data)
    return bytes(data)


def to_mode2(iso_path, bin_path):
    """Convierte una ISO de 2048 bytes/sector en una imagen MODE2/2352 (forma 1)."""
    with open(iso_path, "rb") as f:
        iso = f.read()
    out = bytearray()
    for i in range(len(iso) // 2048):
        out += pops.SYNC + bytes(4) + bytes(8) + iso[i * 2048:(i + 1) * 2048] + bytes(280)
    with open(bin_path, "wb") as f:
        f.write(out)


def write(path, text):
    with open(path, "w", encoding="latin-1") as f:
        f.write(text)


class TestHeader(unittest.TestCase):
    def test_bcd_msf(self):
        self.assertEqual(pops._msf(0), (0x00, 0x00, 0x00))
        self.assertEqual(pops._msf(150), (0x00, 0x02, 0x00))
        self.assertEqual(pops._msf(9 * 4500 + 59 * 75 + 74), (0x09, 0x59, 0x74))
        self.assertEqual(pops._msf(9 * 4500 + 59 * 75 + 74 + 1), (0x10, 0x00, 0x00))   # acarreo BCD

    def test_single_track(self):
        with tempfile.TemporaryDirectory() as d:
            data = raw_bin(os.path.join(d, "Juego.bin"), 1000)
            write(os.path.join(d, "Juego.cue"),
                  'FILE "Juego.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
            out = os.path.join(d, "Juego.VCD")
            pops.convert([os.path.join(d, "Juego.cue"), os.path.join(d, "Juego.bin")], out)
            with open(out, "rb") as f:
                vcd = f.read()
        h = vcd[:pops.HEADER]
        self.assertEqual(len(vcd), pops.HEADER + 1000 * S)
        self.assertEqual(vcd[pops.HEADER:], data)
        self.assertEqual(h[0:10], bytes([0x41, 0, 0xA0, 0, 0, 0, 0, 0x01, 0x20, 0]))         # A0
        self.assertEqual(h[10:20], bytes([0x41, 0, 0xA1, 0, 0, 0, 0, 0x01, 0, 0]))           # A1
        self.assertEqual(h[20:30], bytes([0x41, 0, 0xA2, 0, 0, 0, 0, 0x00, 0x15, 0x25]))     # A2: 1150 = 00:15:25
        self.assertEqual(h[30:40], bytes([0x41, 0, 0x01, 0, 0, 0, 0, 0x00, 0x02, 0x00]))     # pista 1
        self.assertEqual(h[40:50], bytes(10))
        self.assertEqual(h[1024:1028], b"kHn ")
        self.assertEqual(h[1032:1040], (1000).to_bytes(4, "little") * 2)
        self.assertEqual(h[1040:], bytes(pops.HEADER - 1040))

    def test_redump_split_tracks(self):
        """Una pista por archivo (Redump): se une sin archivos intermedios."""
        with tempfile.TemporaryDirectory() as d:
            t1 = raw_bin(os.path.join(d, "Juego (Track 1).bin"), 3000)
            t2 = raw_bin(os.path.join(d, "Juego (Track 2).bin"), 500, sync=False)
            write(os.path.join(d, "Juego.cue"),
                  'FILE "Juego (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
                  'FILE "Juego (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n    INDEX 00 00:00:00\n'
                  '    INDEX 01 00:02:00\n')
            out = os.path.join(d, "x.VCD")
            pops.convert([os.path.join(d, n) for n in sorted(os.listdir(d))], out)
            with open(out, "rb") as f:
                vcd = f.read()
        h = vcd[:pops.HEADER]
        self.assertEqual(vcd[pops.HEADER:], t1 + t2)
        self.assertEqual((h[10], h[17], h[20]), (0x01, 0x02, 0x01))                        # última: audio, pista 2
        self.assertEqual(h[27:30], bytes([0x00, 0x48, 0x50]))                              # 3650 = 00:48:50
        self.assertEqual(h[40:50], bytes([0x01, 0, 0x02, 0x00, 0x42, 0x00, 0, 0x00, 0x44, 0x00]))
        self.assertEqual(h[1032:1036], (3500).to_bytes(4, "little"))

    def test_cdrwin_pregap_inserted(self):
        """Cue de CDRWIN (PREGAP que no está en el .bin): se insertan 150 sectores vacíos."""
        with tempfile.TemporaryDirectory() as d:
            data = raw_bin(os.path.join(d, "J.bin"), 3500)
            write(os.path.join(d, "J.cue"),
                  'FILE "J.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
                  '  TRACK 02 AUDIO\n    PREGAP 00:02:00\n    INDEX 01 00:40:00\n')
            out = os.path.join(d, "J.VCD")
            pops.convert([os.path.join(d, "J.cue"), os.path.join(d, "J.bin")], out)
            with open(out, "rb") as f:
                vcd = f.read()
        h, body = vcd[:pops.HEADER], vcd[pops.HEADER:]
        self.assertEqual(body, data[:3000 * S] + bytes(150 * S) + data[3000 * S:])
        self.assertEqual(h[40:50], bytes([0x01, 0, 0x02, 0x00, 0x44, 0x00, 0, 0x00, 0x44, 0x00]))
        self.assertEqual(h[27:30], bytes([0x00, 0x50, 0x50]))                              # 3800 = 00:50:50
        self.assertEqual(h[1032:1036], (3650).to_bytes(4, "little"))

    def test_single_bin_without_cue(self):
        with tempfile.TemporaryDirectory() as d:
            raw_bin(os.path.join(d, "J.bin"), 10)
            plan = pops.plan_for([os.path.join(d, "J.bin")])
            self.assertEqual(len(plan["tracks"]), 1)


class TestRejects(unittest.TestCase):
    def _cue(self, d, text, sectors=10, sync=True):
        raw_bin(os.path.join(d, "J.bin"), sectors, sync=sync)
        write(os.path.join(d, "J.cue"), text)
        return [os.path.join(d, "J.cue"), os.path.join(d, "J.bin")]

    def test_rejected(self):
        bad = {
            "wave": 'FILE "J.bin" WAVE\n TRACK 01 MODE2/2352\n INDEX 01 00:00:00\n',
            "mode1 primero": 'FILE "J.bin" BINARY\n TRACK 01 MODE1/2048\n INDEX 01 00:00:00\n',
            "sin index": 'FILE "J.bin" BINARY\n TRACK 01 MODE2/2352\n',
            "falta archivo": 'FILE "Otro.bin" BINARY\n TRACK 01 MODE2/2352\n INDEX 01 00:00:00\n',
            "fuera de carpeta": 'FILE "../../etc/passwd" BINARY\n TRACK 01 MODE2/2352\n INDEX 01 00:00:00\n',
            "postgap": 'FILE "J.bin" BINARY\n TRACK 01 MODE2/2352\n INDEX 01 00:00:00\n POSTGAP 00:02:00\n',
        }
        for why, text in bad.items():
            with self.subTest(why), tempfile.TemporaryDirectory() as d:
                with self.assertRaises(pops.CueError):
                    pops.plan_for(self._cue(d, text))

    def test_iso_2048_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            files = self._cue(d, 'FILE "J.bin" BINARY\n TRACK 01 MODE2/2352\n INDEX 01 00:00:00\n', sync=False)
            self.assertFalse(pops.can_convert(files))

    def test_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            files = self._cue(d, 'FILE "J.bin" BINARY\n TRACK 01 MODE2/2352\n INDEX 01 00:00:00\n')
            out = os.path.join(d, "J.VCD")
            write(out, "existente")
            with self.assertRaises(FileExistsError):
                pops.convert(files, out)
            with open(out) as f:
                self.assertEqual(f.read(), "existente")


class TestImportPS1(unittest.TestCase):
    def _inbox(self, root):
        game = os.path.join(root, "inbox", "Crash Bandicoot (USA)")
        os.makedirs(game)
        iso = os.path.join(root, "t.iso")
        make_iso(iso, b"BOOT = cdrom:\\SCUS_949.00;1\r\nTCB = 4\r\n")
        to_mode2(iso, os.path.join(game, "Crash Bandicoot (USA).bin"))
        write(os.path.join(game, "Crash Bandicoot (USA).cue"),
              'FILE "Crash Bandicoot (USA).bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
        return os.path.join(root, "inbox")

    def test_import_converts_to_vcd(self):
        with tempfile.TemporaryDirectory() as root:
            lib = os.path.join(root, "lib")
            os.makedirs(lib)
            items = core.scan_inbox(self._inbox(root), lib)
            self.assertEqual(len(items), 1)
            it = items[0]
            self.assertEqual((it.console, it.gid), ("PS1", "SCUS_949.00"))
            cfg = {"library": lib, "download_art": False, "max_name": 32}
            self.assertEqual(core.item_destination(it, cfg),
                             ("vcd", "POPS", "SCUS_949.00.Crash Bandicoot (USA).VCD"))
            copied, _art = core.import_item(it, cfg)
            self.assertEqual(os.listdir(os.path.join(lib, "POPS")), ["SCUS_949.00.Crash Bandicoot (USA).VCD"])
            # la biblioteca lo reconoce como juego de PS1 con su código (también leyendo el VCD)
            games = core.scan_library(lib)
            self.assertEqual([(g.kind, g.gid, g.console) for g in games], [("POPS", "SCUS_949.00", "PS1")])
            os.rename(copied[0], os.path.join(lib, "POPS", "Crash.VCD"))
            core._id_cache = {}
            self.assertEqual(core.disc_info(os.path.join(lib, "POPS", "Crash.VCD")), ("PS1", "SCUS_949.00"))
            # y la bandeja ya lo marca como copiado
            self.assertTrue(core.scan_inbox(os.path.join(root, "inbox"), lib)[0].in_library)

    def test_copy_mode_keeps_bin_cue(self):
        with tempfile.TemporaryDirectory() as root:
            lib = os.path.join(root, "lib")
            os.makedirs(lib)
            it = core.scan_inbox(self._inbox(root), lib)[0]
            cfg = {"library": lib, "download_art": False, "ps1_mode": "copy", "ps1_dir": "../../fuera"}
            action, folder, _n = core.item_destination(it, cfg)
            self.assertEqual((action, folder), ("copy", "fuera"))       # la carpeta nunca sale de la biblioteca
            core.import_item(it, cfg)
            self.assertEqual(sorted(os.listdir(os.path.join(lib, "fuera"))),
                             ["Crash Bandicoot (USA).bin", "Crash Bandicoot (USA).cue"])

    def test_ps1_art_url(self):
        self.assertIn("/PS1/SCUS_949.00/SCUS_949.00_COV.png",
                      core.ART_BASE.format(console="PS1", id="SCUS_949.00", t="COV"))


if __name__ == "__main__":
    unittest.main()
