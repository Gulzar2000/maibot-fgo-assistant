"""跨电脑绑定文件：RSA-OAEP + AES-GCM，仅机器人持有私钥。"""
from pathlib import Path
from typing import Dict
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import base64
import hashlib
import json

from .account_protocol.errors import ExportError
from .credentials import atomic_write, json_bytes, protect, validate_credentials
from .pool_manager import normalize_qq_user_id

FORMAT = "maibot.fgo-binding.v1"


def prepare_key(private_path: Path, public_path: Path) -> None:
    """仅构建工具时调用；插件运行期间不自动生成或更换导入密钥。"""
    if private_path.exists():
        key = serialization.load_pem_private_key(protect(private_path.read_bytes(), True), password=None)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        raw = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        atomic_write(private_path, protect(raw))
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    atomic_write(public_path, public)


def open_binding(raw: bytes, private_path: Path) -> Dict:
    if not 1 <= len(raw) <= 65536:
        raise ExportError("BINDING_FILE_SIZE_INVALID")
    try:
        envelope = json.loads(raw)
        if envelope["format"] != FORMAT:
            raise ExportError("BINDING_FORMAT_INVALID")
        key = serialization.load_pem_private_key(protect(private_path.read_bytes(), True), password=None)
        public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        kid = hashlib.sha256(public).hexdigest()
        if envelope["key_id"] != kid:
            raise ExportError("BINDING_WRONG_BOT_KEY")
        aes_key = key.decrypt(base64.b64decode(envelope["wrapped_key"], validate=True),
                              padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        plaintext = AESGCM(aes_key).decrypt(base64.b64decode(envelope["nonce"], validate=True),
                                          base64.b64decode(envelope["payload"], validate=True),
                                          (FORMAT + ":" + kid).encode())
        value = json.loads(plaintext)
        if value["version"] != 1 or value["server"] not in ("android", "ios", "uo"):
            raise ExportError("BINDING_PAYLOAD_INVALID")
        qq = normalize_qq_user_id(value["qq"])
        validate_credentials(value["user"])
        user = {name: value["user"][name] for name in ("uid", "access_key", "uname", "username") if name in value["user"]}
        return dict(qq=qq, server=value["server"], user=user)
    except ExportError:
        raise
    except Exception:
        raise ExportError("BINDING_FILE_INVALID_OR_TAMPERED") from None
