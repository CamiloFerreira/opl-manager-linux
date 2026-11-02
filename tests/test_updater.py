"""Pruebas del actualizador (sin internet: usa URLs file://)."""
import hashlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import updater  # noqa: E402


class TestVersions(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(updater.is_newer("v0.2.0", "0.1.0"))
        self.assertTrue(updater.is_newer("0.10.0", "0.9.9"))
        self.assertFalse(updater.is_newer("v0.1.0", "0.1.0"))
        self.assertFalse(updater.is_newer("0.1.0", "0.2.0"))

    def test_should_check(self):
        self.assertTrue(updater.should_check({"auto_update": True, "last_update_check": 0}))
        self.assertFalse(updater.should_check({"auto_update": False, "last_update_check": 0}))


class TestSelfUpdate(unittest.TestCase):
    def _release(self, d, payload, good_sum=True):
        asset = os.path.join(d, "opl-manager")
        with open(asset, "wb") as f:
            f.write(payload)
        digest = hashlib.sha256(payload if good_sum else b"otra cosa").hexdigest()
        sums = os.path.join(d, "SHA256SUMS")
        with open(sums, "w") as f:
            f.write(f"{digest}  opl-manager\n")
        return {"tag": "v9.9.9", "version": "9.9.9", "url": "", "notes": "",
                "assets": {"opl-manager": "file://" + asset, "SHA256SUMS": "file://" + sums}}

    def test_replaces_executable(self):
        with tempfile.TemporaryDirectory() as srv, tempfile.TemporaryDirectory() as inst:
            exe = os.path.join(inst, "opl-manager")
            with open(exe, "wb") as f:
                f.write(b"vieja")
            rel = self._release(srv, b"#!/usr/bin/env python3\nnueva")
            with mock.patch.object(updater, "executable_path", return_value=exe):
                self.assertEqual(updater.install_mode(), "self")
                self.assertEqual(updater.apply_update(rel), "restart")
            with open(exe, "rb") as f:
                self.assertEqual(f.read(), b"#!/usr/bin/env python3\nnueva")
            self.assertTrue(os.access(exe, os.X_OK))
            self.assertEqual(os.listdir(inst), ["opl-manager"])        # sin restos temporales

    def test_bad_checksum_keeps_old(self):
        with tempfile.TemporaryDirectory() as srv, tempfile.TemporaryDirectory() as inst:
            exe = os.path.join(inst, "opl-manager")
            with open(exe, "wb") as f:
                f.write(b"vieja")
            rel = self._release(srv, b"manipulada", good_sum=False)
            with mock.patch.object(updater, "executable_path", return_value=exe):
                with self.assertRaises(RuntimeError):
                    updater.apply_update(rel)
            with open(exe, "rb") as f:
                self.assertEqual(f.read(), b"vieja")

    def test_source_mode(self):
        with mock.patch.object(updater, "executable_path", return_value=""):
            self.assertEqual(updater.install_mode(), "source")


if __name__ == "__main__":
    unittest.main()
