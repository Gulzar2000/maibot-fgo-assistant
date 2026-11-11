"""本地导入使用合成凭据，覆盖加密、QQ 归属和解绑后的重放行为。"""
from pathlib import Path
from threading import Event
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import base64
import hashlib
import json
import os
import tempfile
import unittest

from test_pool_query import ROOT
from gacha.accounts import AccountStore
from gacha.binding_transfer import FORMAT, open_binding, prepare_key
from gacha.account_protocol.errors import ExportError


class BindingTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.private = self.root / "private/import-private.dpapi"
        self.public = self.root / "public.pem"
        prepare_key(self.private, self.public)
        self.store = AccountStore(self.root / "bindings", self.root / "private", ROOT / "res/gacha_metadata_cn.json")
        self.store.import_dir.mkdir()
        self.user = dict(uid="10001", access_key="synthetic-transfer-credential")

    def artifact(self, qq="12345678", server="ios", private_key=None):
        public = private_key.public_key() if private_key else serialization.load_pem_public_key(self.public.read_bytes())
        der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        kid = hashlib.sha256(der).hexdigest()
        aes_key, nonce = os.urandom(32), os.urandom(12)
        body = json.dumps(dict(version=1, qq=qq, server=server, user=self.user)).encode()
        ciphertext = AESGCM(aes_key).encrypt(nonce, body, (FORMAT + ":" + kid).encode())
        encode = lambda value: base64.b64encode(value).decode()
        return json.dumps(dict(format=FORMAT, key_id=kid, nonce=encode(nonce), payload=encode(ciphertext),
            wrapped_key=encode(public.encrypt(aes_key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                                    algorithm=hashes.SHA256(), label=None))))).encode()

    def incoming(self, raw):
        path = self.store.import_dir / "12345678.fgobind"
        path.write_bytes(raw)
        return path

    def test_encrypted_roundtrip_automatic_import_once_then_unbind(self):
        raw = self.artifact()
        self.assertNotIn(b"synthetic-transfer-credential", raw)
        self.assertEqual(open_binding(raw, self.private)["user"], self.user)
        path = self.incoming(raw)
        value = self.store.load("12345678")
        self.assertEqual(value, dict(qq="12345678", server="ios", user=self.user))
        self.assertFalse(path.exists())
        self.assertTrue((self.store.import_dir / "12345678.imported").exists())
        self.assertEqual(self.store.load("12345678"), value)
        self.assertIsNone(self.store.load("23456789"))
        self.store.delete("12345678")
        self.assertIsNone(self.store.load("12345678"))
        self.assertFalse((self.store.import_dir / "12345678.imported").exists())

    def test_corrupted_or_wrong_qq_file_does_not_replace_existing_binding(self):
        self.store.save("12345678", "android", self.user, Event())
        before = self.store.path("12345678").read_bytes()
        for raw in (b"invalid", self.artifact(qq="23456789"), self.artifact(server="invalid")):
            path = self.incoming(raw)
            with self.assertRaises(ExportError):
                self.store.load("12345678")
            self.assertEqual(self.store.path("12345678").read_bytes(), before)
            self.assertTrue(path.exists())

    def test_authentication_tag_and_wrong_bot_key_rejected(self):
        envelope = json.loads(self.artifact())
        ciphertext = bytearray(base64.b64decode(envelope["payload"]))
        ciphertext[-1] ^= 1
        envelope["payload"] = base64.b64encode(ciphertext).decode()
        with self.assertRaises(ExportError):
            open_binding(json.dumps(envelope).encode(), self.private)
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaisesRegex(ExportError, "BINDING_WRONG_BOT_KEY"):
            open_binding(self.artifact(private_key=other), self.private)

    def test_unbind_removes_pending_import_without_recreating_binding(self):
        self.incoming(self.artifact())
        self.store.delete("12345678")
        self.assertIsNone(self.store.load("12345678"))

    def test_rebuilding_tool_preserves_the_existing_key(self):
        public = self.public.read_bytes()
        raw = self.artifact()
        prepare_key(self.private, self.public)
        self.assertEqual(self.public.read_bytes(), public)
        self.assertEqual(open_binding(raw, self.private)["qq"], "12345678")

    def test_size_limit_and_plaintext_credentials_rejected(self):
        for raw in (b"x" * 65537, json.dumps(dict(qq="12345678", user=self.user)).encode()):
            with self.assertRaises(ExportError):
                open_binding(raw, self.private)
