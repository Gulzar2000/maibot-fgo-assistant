"""独立工具后端；标准输出仅包含界面进度和二维码，不输出登录凭据。"""
from pathlib import Path
from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import time

from account_protocol.errors import ExportError
from account_protocol.game_profile import load_profile
from account_protocol.protocol import SERVERS, GameClient

FORMAT = "maibot.fgo-binding.v1"
SERVERS["uo"] = ("享游安卓", "https://line3-s1-uo-fate.bilibiligame.net")


def seal(qq: str, server: str, user: dict, public_pem: bytes) -> bytes:
    if not re.fullmatch(r"[1-9][0-9]{4,19}", qq) or server not in SERVERS:
        raise ExportError("QQ_OR_SERVER_INVALID")
    public = RSA.import_key(public_pem)
    if public.has_private():
        raise ExportError("PRIVATE_KEY_MUST_NOT_BE_DISTRIBUTED")
    kid = hashlib.sha256(public.export_key(format="DER")).hexdigest()
    key, nonce = os.urandom(32), os.urandom(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update((FORMAT + ":" + kid).encode())
    value = dict(version=1, qq=qq, server=server, created_at=int(time.time()),
                 user={name: user[name] for name in ("uid", "access_key", "uname", "username") if name in user})
    payload, tag = cipher.encrypt_and_digest(json.dumps(value, ensure_ascii=False).encode())
    encode = lambda raw: base64.b64encode(raw).decode("ascii")
    return json.dumps(dict(format=FORMAT, key_id=kid,
                           wrapped_key=encode(PKCS1_OAEP.new(public, hashAlgo=SHA256).encrypt(key)),
                           nonce=encode(nonce), payload=encode(payload + tag))).encode()


def emit(phase: str, **extra) -> None:
    print(json.dumps(dict(phase=phase, **extra), ensure_ascii=False), flush=True)


def execute(args) -> None:
    base = Path(__file__).resolve().parent
    public = (base / "import-public.pem").read_bytes()
    if args.self_test:
        user = dict(uid="10001", access_key="synthetic-self-test-only")
    else:
        import qrcode

        if not re.fullmatch(r"[1-9][0-9]{4,19}", args.qq):
            raise ExportError("QQ_INVALID")
        filename = "game-protocol-uo.json" if args.server == "uo" else "game-protocol.json"
        profile = load_profile(base / filename)
        if profile is None:
            raise ExportError("LOCAL_PROFILE_REQUIRED")
        client = GameClient(profile, args.server)
        ticket, value = client.generate()
        image = io.BytesIO()
        qrcode.make(value).save(image, format="PNG")
        emit("qr", image=base64.b64encode(image.getvalue()).decode("ascii"))
        value = None
        deadline = time.monotonic() + 180
        user = {}
        while time.monotonic() < deadline:
            phase, user = client.poll(ticket)
            if phase == "authorized":
                break
            if phase == "expired":
                raise ExportError("QR_EXPIRED_RESCAN_REQUIRED")
            emit(phase)
            time.sleep(2)
        else:
            raise ExportError("QR_EXPIRED_RESCAN_REQUIRED")
        emit("checking_account")
        # 验证选中区服存在可登录的角色；响应只在内存中使用，不保存账号抓包。
        client.toplogin(dict(user), lambda phase: None, lambda: False)
    try:
        raw = seal(args.qq, args.server, user, public)
        output = Path(args.output)
        if output.name != args.qq + ".fgobind":
            raise ExportError("OUTPUT_FILENAME_MUST_MATCH_QQ")
        output.parent.mkdir(parents=True, exist_ok=True)
        # 加密完成后直接写入用户选择的产物；无需请求目录删除/重命名权限。
        # 若保存被中断，导入端的 GCM 完整性校验会拒绝不完整文件。
        output.write_bytes(raw)
        emit("complete")
    finally:
        user.clear()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--qq", required=True)
    parser.add_argument("--server", choices=["android", "ios", "uo"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--self-test", action="store_true")
    try:
        execute(parser.parse_args())
    except Exception as error:
        emit("error", code=error.code if isinstance(error, ExportError) else type(error).__name__)
        sys.exit(1)
