"""Pruebas de la lógica de OPL Manager (sin interfaz). Ejecutar: python3 -m unittest -v"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import oplcore as core  # noqa: E402


def make_iso(path, system_cnf):
    """ISO9660 mínima con un SYSTEM.CNF en el directorio raíz."""
    sec = 2048
    data = bytearray(sec * 24)
    pvd = bytearray(sec)
    pvd[0], pvd[1:6], pvd[6] = 1, b"CD001", 1
    root = bytearray(34)
    root[0] = 34
    root[2:6] = (20).to_bytes(4, "little")
    root[10:14] = sec.to_bytes(4, "little")
    root[25], root[32] = 2, 1
    pvd[156:190] = root
    data[16 * sec:17 * sec] = pvd
    name = b"SYSTEM.CNF;1"
    rec = bytearray(33 + len(name) + (len(name) + 1) % 2)
    rec[0] = len(rec)
    rec[2:6] = (21).to_bytes(4, "little")
    rec[10:14] = len(system_cnf).to_bytes(4, "little")
    rec[32] = len(name)
    rec[33:33 + len(name)] = name
    data[20 * sec:20 * sec + len(rec)] = rec
    data[21 * sec:21 * sec + len(system_cnf)] = system_cnf
    with open(path, "wb") as f:
        f.write(data)


class TestNames(unittest.TestCase):
    def test_short_title_strips_tags_and_accents(self):
        self.assertEqual(core.short_title("Resident Evil Code Veronica X [Castellano v1.1] [PAL]"),
                         "Resident Evil Code Veronica X")
        self.assertEqual(core.short_title("[PS2] JoJo's Bizarre Adventure Golden Wind [Español v1.0]"),
                         "JoJo's Bizarre Adventure Golden")
        self.assertEqual(core.short_title("Killer7 [Español] [PAL-NTSC]"), "Killer7 [Espanol] [PAL-NTSC]")

    def test_short_title_limit(self):
        for t in ("Un nombre larguísimo sin etiquetas que no cabe nunca", "GTA Liberty City Stories Latino {NTSC]"):
            self.assertLessEqual(len(core.short_title(t)), 32)

    def test_no_limit(self):
        self.assertEqual(core.short_title("Shaun White Snowboarding [PAL]", 0), "Shaun White Snowboarding [PAL]")

    def test_opl_filename_and_ids(self):
        self.assertEqual(core.opl_filename("SLUS_209.46", "GTA San Andreas", "iso"), "SLUS_209.46.GTA San Andreas.iso")
        self.assertEqual(core.id_from_name("SLES_533.66.Killer7.iso"), "SLES_533.66")
        self.assertEqual(core.id_from_name("Killer7.iso"), "")
        self.assertEqual(core.strip_id("SLES_533.66.Killer7"), "Killer7")


class TestDisc(unittest.TestCase):
    def test_reads_ps2_and_ps1(self):
        with tempfile.TemporaryDirectory() as d:
            ps2, ps1 = os.path.join(d, "a.iso"), os.path.join(d, "b.iso")
            make_iso(ps2, b"BOOT2 = cdrom0:\\SLUS_209.46;1\r\nVER = 1.00\r\n")
            make_iso(ps1, b"BOOT = cdrom:\\SCUS_949.00;1\r\n")
            self.assertEqual(core.disc_info(ps2), ("PS2", "SLUS_209.46"))
            self.assertEqual(core.disc_info(ps1), ("PS1", "SCUS_949.00"))


class TestGrouping(unittest.TestCase):
    def test_group_multi(self):
        with tempfile.TemporaryDirectory() as d:
            files = {"Crash.cue": 'FILE "Crash (Track 1).bin" BINARY\nFILE "Crash (Track 2).bin" BINARY',
                     "Crash (Track 1).bin": "", "Crash (Track 2).bin": "",
                     "Spyro.cue": 'FILE "Spyro.bin" BINARY', "Spyro.bin": "", "Suelto.bin": ""}
            for n, t in files.items():
                with open(os.path.join(d, n), "w") as f:
                    f.write(t)
            groups = sorted(sorted(os.path.basename(x) for x in g)
                            for g in core.group_multi([os.path.join(d, n) for n in files]))
            self.assertEqual(groups, [["Crash (Track 1).bin", "Crash (Track 2).bin", "Crash.cue"],
                                      ["Spyro.bin", "Spyro.cue"], ["Suelto.bin"]])


class TestImport(unittest.TestCase):
    def test_import_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            inbox, lib = os.path.join(d, "in"), os.path.join(d, "lib")
            os.makedirs(os.path.join(inbox, "Juego [PAL]"))
            os.makedirs(lib)
            make_iso(os.path.join(inbox, "Juego [PAL]", "j.iso"), b"BOOT2 = cdrom0:\\SLES_123.45;1\r\n")
            os.utime(os.path.join(inbox, "Juego [PAL]", "j.iso"), (1, 1))
            cfg = dict(core.DEFAULT_CONFIG, library=lib, download_art=False)
            items, missing = core.scan_inboxes([inbox], lib)
            self.assertEqual(missing, [])
            self.assertEqual(len(items), 1)
            copied, _ = core.import_item(items[0], cfg)
            self.assertEqual(os.path.basename(copied[0]), "SLES_123.45.Juego [PAL].iso")
            again, _ = core.scan_inboxes([inbox], lib)
            self.assertTrue(again[0].in_library)
            with self.assertRaises(FileExistsError):
                core.import_item(again[0], cfg)


class TestCleanup(unittest.TestCase):
    def test_targets(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "in")
            # juego en su carpeta (se borra la carpeta), juego suelto en DVD/ (solo el archivo),
            # carpeta con dos juegos de los que solo se limpia uno (solo su archivo)
            os.makedirs(os.path.join(inbox, "Killer7 [PAL]"))
            os.makedirs(os.path.join(inbox, "DVD"))
            os.makedirs(os.path.join(inbox, "Pack"))
            for rel in ("Killer7 [PAL]/k.iso", "DVD/SLUS_209.95.KOF.iso", "Pack/a.iso", "Pack/b.iso"):
                make_iso(os.path.join(inbox, rel), b"BOOT2 = cdrom0:\\SLES_111.11;1\r\n")
                os.utime(os.path.join(inbox, rel), (1, 1))
            with open(os.path.join(inbox, "Killer7 [PAL]", "LEEME.txt"), "w") as f:
                f.write("x")
            items, _ = core.scan_inboxes([inbox], "")
            pick = [i for i in items if os.path.basename(i.main) in ("k.iso", "SLUS_209.95.KOF.iso", "a.iso")]
            got = sorted(os.path.relpath(t, inbox) for t in core.cleanup_targets(pick))
            self.assertEqual(got, ["DVD/SLUS_209.95.KOF.iso", "Killer7 [PAL]", "Pack/a.iso"])
            both = [i for i in items if os.path.dirname(i.main).endswith("Pack")]
            self.assertEqual([os.path.relpath(t, inbox) for t in core.cleanup_targets(both)], ["Pack"])


if __name__ == "__main__":
    unittest.main()
