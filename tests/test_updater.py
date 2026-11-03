"""Pruebas del actualizador (sin internet: las descargas se simulan)."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import updater  # noqa: E402

BASE = updater.ASSET_PREFIX + "v9.9.9/"


# ---- firma Ed25519 (RFC 8032) solo para las pruebas, con una clave de prueba
def _keypair(seed):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:], _compress(updater._pmul(a, updater._G))


def _compress(pt):
    zi = pow(pt[2], updater._P - 2, updater._P)
    x, y = pt[0] * zi % updater._P, pt[1] * zi % updater._P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _sign(seed, msg):
    a, prefix, pub = _keypair(seed)
    r = int.from_bytes(hashlib.sha512(prefix + msg).digest(), "little") % updater._L
    rs = _compress(updater._pmul(r, updater._G))
    h = int.from_bytes(hashlib.sha512(rs + pub + msg).digest(), "little") % updater._L
    return rs + int.to_bytes((r + h * a) % updater._L, 32, "little")


SEED = b"clave-de-prueba-no-usar".ljust(32, b"0")
TEST_PUB = _keypair(SEED)[2]


def make_release(payload, version="9.9.9", min_safe="0.0.0", good_sum=True, signed=True,
                 sign_seed=SEED, manifest_version=None):
    """Devuelve (rel sin verificar, {url: bytes}) como lo daría GitHub."""
    manifest = json.dumps({"version": manifest_version or version, "min_safe_version": min_safe,
                           "advisory": "prueba"}).encode()
    digest = hashlib.sha256(payload if good_sum else b"otra cosa").hexdigest()
    sums = (f"{digest}  opl-manager\n"
            f"{hashlib.sha256(manifest).hexdigest()}  release.json\n").encode()
    files = {"opl-manager": payload, "SHA256SUMS": sums, "release.json": manifest}
    if signed:
        files["SHA256SUMS.sig"] = _sign(sign_seed, sums)
    web = {BASE + n: d for n, d in files.items()}
    rel = {"tag": "v" + version, "version": version, "url": "", "notes": "",
           "assets": {n: BASE + n for n in files}}
    return rel, web


class Fake:
    def __init__(self, web):
        self.web = web

    def __call__(self, url, limit, timeout=20):
        data = self.web[url]
        if len(data) > limit:
            raise updater.UpdateError("grande")
        return data


class TestEd25519(unittest.TestCase):
    def test_rfc8032_vector(self):
        pk = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bac"
                            "c61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertTrue(updater.ed25519_verify(pk, b"", sig))
        self.assertFalse(updater.ed25519_verify(pk, b"x", sig))

    def test_sign_roundtrip(self):
        sig = _sign(SEED, b"hola")
        self.assertTrue(updater.ed25519_verify(TEST_PUB, b"hola", sig))
        self.assertFalse(updater.ed25519_verify(TEST_PUB, b"hola!", sig))
        self.assertFalse(updater.ed25519_verify(TEST_PUB, b"hola", sig[:-1] + bytes([sig[-1] ^ 1])))


class TestVersions(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(updater.is_newer("v0.2.0", "0.1.0"))
        self.assertTrue(updater.is_newer("0.10.0", "0.9.9"))
        self.assertFalse(updater.is_newer("v0.1.0", "0.1.0"))
        self.assertFalse(updater.is_newer("0.1.0", "0.2.0"))

    def test_should_check(self):
        self.assertTrue(updater.should_check({"auto_update": True, "last_update_check": 0}))
        # con las automáticas apagadas igual se consulta (por si hay una de seguridad)
        self.assertTrue(updater.should_check({"auto_update": False, "last_update_check": 0}))
        import time
        self.assertFalse(updater.should_check({"last_update_check": time.time()}))
        self.assertTrue(updater.should_check({"last_update_check": time.time(), "security_update_pending": "1.0.0"}))

    def test_mandatory(self):
        rel = {"version": "0.5.0", "min_safe_version": "0.4.0"}
        self.assertTrue(updater.is_mandatory(rel, "0.3.1"))
        self.assertFalse(updater.is_mandatory(rel, "0.4.0"))
        self.assertFalse(updater.is_mandatory({"version": "0.5.0", "min_safe_version": "0.0.0"}, "0.3.1"))


@mock.patch.object(updater, "RELEASE_PUBKEY", TEST_PUB)
class TestVerify(unittest.TestCase):
    def test_valid_release(self):
        rel, web = make_release(b"x", min_safe="9.9.9")
        with mock.patch.object(updater, "_get", Fake(web)):
            updater.verify_release(rel)
        self.assertEqual(rel["min_safe_version"], "9.9.9")
        self.assertTrue(updater.is_mandatory(rel, "0.4.0"))

    def test_unsigned_rejected(self):
        rel, web = make_release(b"x", signed=False)
        with mock.patch.object(updater, "_get", Fake(web)):
            with self.assertRaises(updater.UpdateError):
                updater.verify_release(rel)

    def test_wrong_key_rejected(self):
        rel, web = make_release(b"x", sign_seed=b"otra-clave".ljust(32, b"0"))
        with mock.patch.object(updater, "_get", Fake(web)):
            with self.assertRaises(updater.UpdateError):
                updater.verify_release(rel)

    def test_manifest_version_mismatch(self):
        rel, web = make_release(b"x", manifest_version="1.0.0")
        with mock.patch.object(updater, "_get", Fake(web)):
            with self.assertRaises(updater.UpdateError):
                updater.verify_release(rel)

    def test_latest_release_validates_api(self):
        api = {"tag_name": "v1.2.3/../../x", "html_url": "https://evil.example/", "assets": []}
        with mock.patch.object(updater, "_get", return_value=json.dumps(api).encode()):
            with self.assertRaises(updater.UpdateError):
                updater.latest_release()

    def test_latest_release_filters_foreign_assets(self):
        rel, web = make_release(b"x")
        api = {"tag_name": "v9.9.9", "html_url": "https://evil.example/",
               "assets": [{"name": n, "browser_download_url": u} for n, u in rel["assets"].items()] +
                         [{"name": "malo", "browser_download_url": "file:///etc/passwd"}]}
        web[updater.API_LATEST] = json.dumps(api).encode()
        with mock.patch.object(updater, "_get", Fake(web)):
            got = updater.latest_release()
        self.assertNotIn("malo", got["assets"])
        self.assertTrue(got["url"].startswith(updater.RELEASE_PAGE_PREFIX))

    def test_https_only(self):
        with self.assertRaises(updater.UpdateError):
            updater._get("file:///etc/passwd", 100)
        with self.assertRaises(updater.UpdateError):
            updater._get("http://example.com/", 100)


@mock.patch.object(updater, "RELEASE_PUBKEY", TEST_PUB)
class TestSelfUpdate(unittest.TestCase):
    def _run(self, rel, web, local="0.1.0"):
        with tempfile.TemporaryDirectory() as inst:
            exe = os.path.join(inst, "opl-manager")
            with open(exe, "wb") as f:
                f.write(b"vieja")
            err = None
            with mock.patch.object(updater, "executable_path", return_value=exe), \
                    mock.patch.object(updater, "_get", Fake(web)):
                self.assertEqual(updater.install_mode(), "self")
                try:
                    self.assertEqual(updater.apply_update(rel, local), "restart")
                except updater.UpdateError as e:
                    err = e
            with open(exe, "rb") as f:
                content = f.read()
            return content, err, os.access(exe, os.X_OK), sorted(os.listdir(inst))

    def test_replaces_executable(self):
        rel, web = make_release(b"#!/usr/bin/env python3\nnueva")
        content, err, x, files = self._run(rel, web)
        self.assertIsNone(err)
        self.assertEqual(content, b"#!/usr/bin/env python3\nnueva")
        self.assertTrue(x)
        self.assertEqual(files, ["opl-manager"])        # sin restos temporales

    def test_bad_checksum_keeps_old(self):
        rel, web = make_release(b"manipulada", good_sum=False)
        content, err, _x, _f = self._run(rel, web)
        self.assertIsInstance(err, updater.UpdateError)
        self.assertEqual(content, b"vieja")

    def test_unsigned_keeps_old(self):
        rel, web = make_release(b"manipulada", signed=False)
        content, err, _x, _f = self._run(rel, web)
        self.assertIsInstance(err, updater.UpdateError)
        self.assertEqual(content, b"vieja")

    def test_no_downgrade(self):
        rel, web = make_release(b"vieja-version", version="0.0.9")
        content, err, _x, _f = self._run(rel, web, local="0.4.0")
        self.assertIsInstance(err, updater.UpdateError)
        self.assertEqual(content, b"vieja")

    def test_source_mode(self):
        with mock.patch.object(updater, "executable_path", return_value=""):
            self.assertEqual(updater.install_mode(), "source")


if __name__ == "__main__":
    unittest.main()
