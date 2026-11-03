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
    def test_system_cnf_with_tabs(self):
        # Champions of Norrath (PAL): "BOOT2\t= cdrom0:\\SLES_523.73;1"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "Champions.iso")
            make_iso(p, b"BOOT2\t= cdrom0:\\SLES_523.73;1\r\nVER\t= 1.02\r\nVMODE\t= PAL\r\n")
            self.assertEqual(core.disc_info(p), ("PS2", "SLES_523.73"))

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


class TestSecurity(unittest.TestCase):
    def test_filename_never_escapes_folder(self):
        for title in ("/", "///", "..", "../../etc/passwd", "", "\x00", "?*<>"):
            for ml in (32, 0):
                name = core.opl_filename("SLUS_209.46", title, "iso", ml)
                self.assertNotIn("/", name)
                self.assertFalse(name.startswith("."))
                self.assertEqual(os.path.basename(name), name)
        self.assertEqual(core.opl_filename("../../x", "Juego", "iso"), "Juego.iso")   # código inválido: se ignora

    def test_valid_gid(self):
        self.assertTrue(core.valid_gid("SLUS_209.46"))
        for bad in ("", "SLUS_209.46/..", "../SLUS_209.46", "slus_209.46"):
            self.assertFalse(core.valid_gid(bad))
        self.assertEqual(core.download_art("/nonexistent", "../../x"), [])

    def test_malformed_iso_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "malo.iso")
            data = bytearray(2048 * 20)
            pvd = 16 * 2048
            data[pvd + 1:pvd + 6] = b"CD001"
            root = pvd + 156
            data[root + 2:root + 6] = (17).to_bytes(4, "little")
            data[root + 10:root + 14] = (2048).to_bytes(4, "little")
            for i in range(0, 2040, 255):         # registros encadenados hasta el final del sector...
                data[17 * 2048 + i] = 255
            data[17 * 2048 + 2040] = 200          # ...y uno que se sale de los datos leídos
            with open(p, "wb") as f:
                f.write(data)
            self.assertEqual(core._read_boot_line(p), "")

    def test_inbox_art_symlink_not_copied(self):
        with tempfile.TemporaryDirectory() as d:
            secret = os.path.join(d, "secreto.png")
            with open(secret, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\nprivado")
            link = os.path.join(d, "link.png")
            os.symlink(secret, link)
            self.assertFalse(core._is_plain_png(link))
            self.assertTrue(core._is_plain_png(secret))


class TestNameFixes(unittest.TestCase):
    def _lib(self, d, files):
        for rel in files:
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"x")

    def _game(self, d, rel, gid):
        kind = rel.split("/")[0]
        stem = os.path.splitext(os.path.basename(rel))[0]
        return core.Game(os.path.join(d, rel), kind, gid, core.strip_id(stem), 1, d)

    def test_clean_title(self):
        self.assertEqual(core.clean_title("God of War [SLUS-20678]"), "God of War")
        self.assertEqual(core.clean_title("Killer7 (SLES_533.66) [PAL]"), "Killer7 [PAL]")
        self.assertEqual(core.clean_title("Silent_Hill_2"), "Silent Hill 2")
        self.assertEqual(core.clean_title("Tekken 5"), "Tekken 5")

    def test_detects_and_renames(self):
        with tempfile.TemporaryDirectory() as d:
            self._lib(d, ["DVD/God of War [SLUS-20678].iso", "DVD/SLUS_209.46.GTA San Andreas.iso",
                          "DVD/SLES-533.66 Killer7.iso", "POPS/Crash.VCD", "POPS/Crash/SLOT0.VMC"])
            games = [self._game(d, "DVD/God of War [SLUS-20678].iso", "SLUS_206.78"),
                     self._game(d, "DVD/SLUS_209.46.GTA San Andreas.iso", "SLUS_209.46"),
                     self._game(d, "DVD/SLES-533.66 Killer7.iso", "SLES_533.66"),
                     self._game(d, "POPS/Crash.VCD", "SCUS_944.26")]
            fixes = core.name_fixes(games)
            self.assertEqual(sorted(f.new_name for f in fixes),
                             ["SCUS_944.26.Crash.VCD", "SLES_533.66.Killer7.iso", "SLUS_206.78.God of War.iso"])
            done, errors = core.apply_name_fixes(fixes)
            self.assertEqual((done, errors), (3, []))
            self.assertTrue(os.path.isfile(os.path.join(d, "DVD", "SLUS_206.78.God of War.iso")))
            # la carpeta de partidas de POPS acompaña al .VCD
            self.assertTrue(os.path.isfile(os.path.join(d, "POPS", "SCUS_944.26.Crash", "SLOT0.VMC")))
            self.assertEqual(core.name_fixes(core.scan_library(d)), [])

    def test_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            self._lib(d, ["DVD/Tekken 5.iso", "DVD/SLUS_210.59.Tekken 5.iso", "DVD/Tekken_5.iso"])
            games = [self._game(d, "DVD/Tekken 5.iso", "SLUS_210.59"),
                     self._game(d, "DVD/Tekken_5.iso", "SLUS_210.59")]
            fixes = core.name_fixes(games)
            self.assertTrue(all(f.problem for f in fixes))
            done, errors = core.apply_name_fixes(fixes)
            self.assertEqual(done, 0)
            self.assertEqual(len(errors), 2)
            self.assertEqual(len(os.listdir(os.path.join(d, "DVD"))), 3)

    def test_same_target_only_once(self):
        with tempfile.TemporaryDirectory() as d:
            self._lib(d, ["DVD/Tekken 5.iso", "DVD/Tekken_5.iso"])
            games = [self._game(d, "DVD/Tekken 5.iso", "SLUS_210.59"),
                     self._game(d, "DVD/Tekken_5.iso", "SLUS_210.59")]
            fixes = core.name_fixes(games)
            self.assertEqual([bool(f.problem) for f in fixes], [False, True])

    def test_rename_rejects_paths(self):
        with tempfile.TemporaryDirectory() as d:
            self._lib(d, ["DVD/a.iso"])
            g = self._game(d, "DVD/a.iso", "")
            for bad in ("../a.iso", "", ".."):
                with self.assertRaises(ValueError):
                    core.rename_to(g, bad)


class TestDiagnostics(unittest.TestCase):
    CNF = b"BOOT2 = cdrom0:\\SLUS_209.46;1\r\nVER = 1.00\r\n"

    def _iso(self, d, rel, cnf=None, vol=None, pad=0):
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        make_iso(p, cnf or self.CNF)
        with open(p, "r+b") as f:
            size = os.path.getsize(p)
            f.seek(16 * 2048 + 80)
            f.write((vol if vol is not None else size // 2048).to_bytes(4, "little"))
        return p

    def _titles(self, issues, path=None):
        return sorted(i.title for i in issues if path is None or i.path == path)

    def test_incomplete_iso(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._iso(d, "DVD/SLUS_209.46.GTA.iso", vol=4000)
            issues = core.diagnose_library(d, core.scan_library(d))
            self.assertIn("Imagen incompleta", self._titles(issues, p))
            inc = [i for i in issues if i.title == "Imagen incompleta"][0]
            self.assertEqual((inc.level, inc.fix), ("error", "trash"))

    def test_cd_game_in_dvd_and_move(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._iso(d, "DVD/SLUS_209.46.Chico.iso")
            g = core.scan_library(d)[0]
            issues = core.diagnose_game(g)
            self.assertEqual([(i.title, i.fix) for i in issues], [("Juego de CD en DVD/", "move_cd")])
            new = core.move_game(g, "CD")
            self.assertEqual(new, os.path.join(d, "CD", "SLUS_209.46.Chico.iso"))
            self.assertFalse(os.path.exists(p))
            self.assertEqual(core.diagnose_game(core.scan_library(d)[0]), [])

    def test_ps1_in_dvd_and_wrong_code(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = self._iso(d, "CD/SLUS_000.01.PS1.iso", cnf=b"BOOT = cdrom:\\SCUS_949.00;1\r\n")
            p2 = self._iso(d, "CD/SLES_111.11.Otro.iso")
            issues = core.diagnose_library(d, core.scan_library(d))
            self.assertIn("Juego de PS1 en CD/", self._titles(issues, p1))
            self.assertIn("El código del nombre no es el del disco", self._titles(issues, p2))

    def test_not_a_disc_and_unreadable_files(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "DVD"))
            os.makedirs(os.path.join(d, "CD"))
            with open(os.path.join(d, "DVD", "basura.iso"), "wb") as f:
                f.write(b"\0" * 100000)
            open(os.path.join(d, "CD", "Juego PS1.bin"), "wb").close()
            issues = core.diagnose_library(d, core.scan_library(d))
            self.assertIn("No es una imagen de disco válida", self._titles(issues))
            self.assertIn("OPL no lee archivos .bin en CD/", self._titles(issues))
            self.assertEqual(issues[0].level, "error")             # lo grave primero

    def test_import_cd_game_goes_to_cd(self):
        it = core.InboxItem(files=["/x/Chico.iso"], console="PS2", gid="SLUS_209.46", title="Chico",
                            size=200 * 1024 * 1024, origin="")
        self.assertEqual(core.item_destination(it, {})[1], "CD")
        it.size = 3 * 1024 ** 3
        self.assertEqual(core.item_destination(it, {})[1], "DVD")

    def test_fragmentation_issues(self):
        g1 = core.Game("/l/DVD/a.iso", "DVD", "", "a", 1, "/l")
        g2 = core.Game("/l/DVD/b.iso", "DVD", "", "b", 1, "/l")
        issues = core.fragmentation_issues([g1, g2], {g1.path: 1, g2.path: 37})
        self.assertEqual([(i.path, i.title) for i in issues], [(g2.path, "Archivo fragmentado (37 trozos)")])

    def test_fragmentation_runs_filefrag(self):
        with tempfile.TemporaryDirectory(dir=os.path.expanduser("~")) as d:
            p = os.path.join(d, "a.iso")
            with open(p, "wb") as f:
                f.write(b"x" * 100000)
            try:
                res = core.fragmentation([p])
            except (FileNotFoundError, core.NeedsRoot):
                self.skipTest("filefrag no disponible o sin FIEMAP aquí")
            self.assertIn(p, res)
            self.assertGreaterEqual(res[p], 1)


if __name__ == "__main__":
    unittest.main()
