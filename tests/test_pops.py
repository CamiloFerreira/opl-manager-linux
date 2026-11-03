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
    def setUp(self):                     # no usar la copia local real de POPSTARTER.ELF / POPS_IOX.PAK
        self._store = tempfile.TemporaryDirectory()
        self._old_store, core.POPS_STORE = core.POPS_STORE, self._store.name

    def tearDown(self):
        core.POPS_STORE = self._old_store
        self._store.cleanup()

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


class TestPopsApps(unittest.TestCase):
    """Accesos en APPS/ para que OPL muestre los juegos de PS1 (como el ejemplo hecho a mano)."""
    ELF = b"\x7fELF" + b"\0" * 4000
    PAK = b"PAK" + b"\0" * 4000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = os.path.join(self.tmp.name, "usb")
        self.store = os.path.join(self.tmp.name, "store")
        self._old_store = core.POPS_STORE
        core.POPS_STORE = self.store
        os.makedirs(os.path.join(self.lib, "POPS"))

    def tearDown(self):
        core.POPS_STORE = self._old_store
        self.tmp.cleanup()

    def _put(self, rel, data=b"x"):
        p = os.path.join(self.lib, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_app_like_manual_example(self):
        self._put("POPS/POPSTARTER.ELF", self.ELF)
        vcd = self._put("POPS/Castlevania_Symphony_of_the_Night.VCD")
        d = core.make_pops_app(self.lib, vcd)
        self.assertEqual(d, os.path.join(self.lib, "APPS", "Castlevania_Symphony_of_the_Night"))
        with open(os.path.join(d, "title.cfg"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "title=Castlevania Symphony of the Night\n"
                                       "boot=XX.Castlevania_Symphony_of_the_Night.ELF\n")
        with open(os.path.join(d, "XX.Castlevania_Symphony_of_the_Night.ELF"), "rb") as f:
            self.assertEqual(f.read(), self.ELF)
        self.assertEqual(core.make_pops_app(self.lib, vcd), "")      # ya estaba: no se toca

    def test_title_without_code(self):
        self._put("POPS/POPSTARTER.ELF", self.ELF)
        vcd = self._put("POPS/SLUS_000.67.Castlevania.VCD")
        d = core.make_pops_app(self.lib, vcd)
        with open(os.path.join(d, "title.cfg"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "title=Castlevania\nboot=XX.SLUS_000.67.Castlevania.ELF\n")

    def test_missing_popstarter(self):
        vcd = self._put("POPS/Juego.VCD")
        with self.assertRaises(FileNotFoundError):
            core.make_pops_app(self.lib, vcd)
        self.assertFalse(os.path.exists(os.path.join(self.lib, "APPS")))

    def test_files_learned_and_copied_to_other_drive(self):
        self._put("POPS/POPSTARTER.ELF", self.ELF)
        self._put("POPS/POPS_IOX.PAK", self.PAK)
        self.assertEqual(sorted(core.learn_pops_files(self.lib)), ["POPSTARTER.ELF", "POPS_IOX.PAK"])
        other = os.path.join(self.tmp.name, "otro")
        os.makedirs(os.path.join(other, "POPS"))
        copied, missing = core.ensure_pops_files(other)
        self.assertEqual((sorted(copied), missing), (["POPSTARTER.ELF", "POPS_IOX.PAK"], []))
        with open(os.path.join(other, "POPS", "POPS_IOX.PAK"), "rb") as f:
            self.assertEqual(f.read(), self.PAK)
        self.assertEqual(core.ensure_pops_files(other), ([], []))     # ya estaban: no se copia de nuevo

    def test_existing_files_never_replaced(self):
        self._put("POPS/popstarter.elf", self.ELF)                    # FAT: mayúsculas da igual
        os.makedirs(self.store)
        with open(os.path.join(self.store, "POPS_IOX.PAK"), "wb") as f:
            f.write(self.PAK)
        copied, missing = core.ensure_pops_files(self.lib)
        self.assertEqual((copied, missing), (["POPS_IOX.PAK"], []))
        self.assertNotIn("POPSTARTER.ELF", os.listdir(os.path.join(self.lib, "POPS")))

    def test_import_rejects_non_elf(self):
        bad = self._put("POPS/cosa.ELF", b"no soy un elf" * 200)
        with self.assertRaises(ValueError):
            core.import_pops_file(bad)
        good = self._put("POPS/otro.elf", self.ELF)
        self.assertEqual(core.import_pops_file(good), "POPSTARTER.ELF")

    def test_rename_moves_app(self):
        self._put("POPS/POPSTARTER.ELF", self.ELF)
        vcd = self._put("POPS/Crash.VCD")
        core.make_pops_app(self.lib, vcd)
        g = core.Game(vcd, "POPS", "SCUS_944.26", "Crash", 1, self.lib)
        new = core.rename_to(g, "SCUS_944.26.Crash Bandicoot.VCD")
        d = os.path.join(self.lib, "APPS", "SCUS_944.26.Crash Bandicoot")
        self.assertTrue(os.path.isfile(os.path.join(d, "XX.SCUS_944.26.Crash Bandicoot.ELF")))
        self.assertFalse(os.path.exists(os.path.join(self.lib, "APPS", "Crash")))
        with open(os.path.join(d, "title.cfg"), encoding="utf-8") as f:
            self.assertIn("boot=XX.SCUS_944.26.Crash Bandicoot.ELF\n", f.read())
        self.assertTrue(core.has_pops_app(self.lib, new))
        self.assertEqual(core.missing_pops_apps(core.scan_library(self.lib)), [])

    def test_app_art_linked_and_follows_rename(self):
        import zlib, struct
        def png():
            raw = b"\0\0\0\0"
            chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        self._put("POPS/POPSTARTER.ELF", self.ELF)
        vcd = self._put("POPS/SCUS_944.26.Crash.VCD")
        self._put("ART/SCUS_944.26_COV.png", png())
        self._put("ART/SCUS_944.26_BG.png", png())
        core.make_pops_app(self.lib, vcd)
        g = core.scan_library(self.lib)[0]
        self.assertEqual(core.sync_pops_app_art([g]), 2)
        art = os.path.join(self.lib, "ART")
        self.assertTrue(os.path.isfile(os.path.join(art, "XX.SCUS_944.26.Crash.ELF_COV.png")))
        self.assertTrue(os.path.isfile(os.path.join(art, "XX.SCUS_944.26.Crash.ELF_BG.png")))
        self.assertEqual(core.sync_pops_app_art([g]), 0)               # no se copia dos veces
        core.rename_to(g, "SCUS_944.26.Crash Bandicoot.VCD")
        self.assertTrue(os.path.isfile(os.path.join(art, "XX.SCUS_944.26.Crash Bandicoot.ELF_COV.png")))
        self.assertFalse(os.path.exists(os.path.join(art, "XX.SCUS_944.26.Crash.ELF_COV.png")))

    def test_smb_prefix_and_switch(self):
        self._put("POPS/POPSTARTER.ELF", self.ELF)
        vcd = self._put("POPS/Castlevania_Symphony_of_the_Night.VCD")
        self._put("ART/XX.Castlevania_Symphony_of_the_Night.ELF_COV.png", b"png")
        d = core.make_pops_app(self.lib, vcd)                       # USB (como el ejemplo hecho a mano)
        g = core.Game(vcd, "POPS", "SLUS_000.67", "x", 1, self.lib)
        self.assertEqual(core.missing_pops_apps([g], "SB"), [g])     # para SMB hay que cambiarlo
        self.assertEqual(core.make_pops_app(self.lib, vcd, "SB"), d)
        self.assertEqual(os.listdir(d).count("SB.Castlevania_Symphony_of_the_Night.ELF"), 1)
        self.assertNotIn("XX.Castlevania_Symphony_of_the_Night.ELF", os.listdir(d))
        with open(os.path.join(d, "title.cfg"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "title=Castlevania Symphony of the Night\n"
                                       "boot=SB.Castlevania_Symphony_of_the_Night.ELF\n")
        art = os.path.join(self.lib, "ART")
        self.assertEqual(os.listdir(art), ["SB.Castlevania_Symphony_of_the_Night.ELF_COV.png"])
        self.assertEqual(core.missing_pops_apps([g], "SB"), [])
        self.assertEqual(core.make_pops_app(self.lib, vcd, "SB"), "")
        issues = core.diagnose_library(self.lib, [g], prefix="XX")
        self.assertIn("Su acceso es para red (SMB)", [i.title for i in issues])
        with self.assertRaises(ValueError):
            core.make_pops_app(self.lib, vcd, "../")

    def test_switch_keeps_custom_title(self):
        self._put("POPS/POPSTARTER.ELF", self.ELF)
        vcd = self._put("POPS/Spyro.VCD")
        d = core.make_pops_app(self.lib, vcd)
        with open(os.path.join(d, "title.cfg"), "w", encoding="utf-8") as f:
            f.write("title=Spyro the Dragon (mío)\nboot=XX.Spyro.ELF\n")
        core.make_pops_app(self.lib, vcd, "SB")
        with open(os.path.join(d, "title.cfg"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "title=Spyro the Dragon (mío)\nboot=SB.Spyro.ELF\n")

    def test_prefix_from_config(self):
        self.assertEqual(core.pops_prefix({}), "XX")
        self.assertEqual(core.pops_prefix({"pops_device": "smb"}), "SB")
        self.assertEqual(core.pops_prefix({"pops_device": "raro"}), "XX")

    def test_smb_network_files(self):
        self.assertEqual(core.smb_config_text("192.168.0.20", 445, "PS2SMB"), "192.168.0.20:445 PS2SMB\r\n\r\n\r\n")
        self.assertEqual(core.smb_config_text("192.168.0.20", "139", "/PS2SMB/", "camilo", "clave"),
                         "192.168.0.20:139 PS2SMB\r\ncamilo\r\nclave\r\n")
        self.assertEqual(core.ip_config_text("192.168.0.10", "255.255.255.0", "192.168.0.1"),
                         "192.168.0.10 255.255.255.0 192.168.0.1\r\n")
        self.assertEqual(core.smb_config_text("192.168.0.254", "", "My Shared Folder"),     # ejemplo oficial
                         "192.168.0.254:445 My Shared Folder\r\n\r\n\r\n")
        for bad in (("300.1.1.1", 445, "S"), ("192.168.0.2", 445, "a/b"), ("192.168.0.2", 0, "S"),
                    ("192.168.0.2", 445, "")):
            with self.assertRaises(ValueError):
                core.smb_config_text(*bad)
        with self.assertRaises(ValueError):
            core.smb_config_text("192.168.0.2", 445, "S", "user\nmal")
        self.assertTrue(core.same_subnet("192.168.0.10", "192.168.0.20", "255.255.255.0"))
        self.assertFalse(core.same_subnet("192.168.2.10", "192.168.0.20", "255.255.255.0"))
        out = core.write_popstarter_net(self.tmp.name, "a\r\n", "b\r\n")
        self.assertEqual(out, os.path.join(self.tmp.name, "POPSTARTER"))
        core.write_popstarter_net(out, "c\r\n", "d\r\n")              # ya era la carpeta POPSTARTER
        with open(os.path.join(out, "SMBCONFIG.DAT"), "rb") as f:
            self.assertEqual(f.read(), b"c\r\n")
        self.assertIn("SMBCONFIG.DAT.bak", os.listdir(out))             # el anterior no se pierde
        with self.assertRaises(ValueError):
            core.write_popstarter_net(self.tmp.name, "contraseña con ñ\r\n", "x")

    def test_smb_checks(self):
        os.makedirs(os.path.join(self.lib, "POPSTARTER"))
        self.assertEqual(len(core.popstarter_net_missing(os.path.join(self.lib, "POPSTARTER"))), 6)
        self._put("POPSTARTER/SMBMAN.IRX")
        self.assertNotIn("smbman.irx", core.popstarter_net_missing(os.path.join(self.lib, "POPSTARTER")))
        self._put("POPS/POPS_IOX.PAK", self.PAK)                      # no es el de red
        vcd = self._put("POPS/Crash.vcd")
        g = core.Game(vcd, "POPS", "", "Crash", 1, self.lib)
        titles = {i.title: i for i in core.diagnose_library(self.lib, [g], prefix="SB")}
        self.assertIn("POPS_IOX.PAK no sirve para red (SMB)", titles)
        self.assertEqual(titles["La extensión tiene que ser .VCD en mayúsculas"].fix, "upper_vcd")
        self.assertNotIn("POPS_IOX.PAK no sirve para red (SMB)",
                         {i.title for i in core.diagnose_library(self.lib, [g], prefix="XX")})


if __name__ == "__main__":
    unittest.main()
