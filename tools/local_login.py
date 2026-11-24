"""独立工具后端；标准输出仅包含界面进度和二维码，不输出登录凭据。"""
from pathlib import Path
from typing import Dict
from Crypto.Cipher import AES, PKCS1_OAEP, PKCS1_v1_5
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
from password_verification import verify_password_login

FORMAT = "maibot.fgo-binding.v1"
_VERIFICATION_CODES = {"200000", "200005", "200007"}
_LOGIN_FIELDS = {
    "account", "app_id", "captcha_category", "captcha_code", "captcha_type", "challenge", "channel_id",
    "ctoken", "device_id", "game_id", "gt_user_id", "hash", "merchant_id", "password", "platform", "pwd",
    "request_id", "sdk_type", "sdk_ver", "seccode", "server_id", "sign", "timestamp", "user_id", "validate", "version",
}
_SDK_ERROR_FIELDS = _LOGIN_FIELDS | {"code", "data", "message", "msg", "need_captch", "requestId", "server_message", "verify_url"}
_SDK_ERROR_WORDS = _LOGIN_FIELDS | {
    "account", "bad", "body", "captcha", "check", "decode", "decrypt", "empty", "error", "expired", "fail", "failed", "field",
    "format", "geetest", "header", "illegal", "incorrect", "invalid", "is", "login", "malformed", "mismatch", "missing", "need", "not",
    "param", "parameter", "parameters", "request", "required", "risk", "rsa", "server", "session", "signature",
    "system", "timeout", "unsupported", "valid", "verification", "verify", "wrong",
}
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


class PasswordLoginError(ExportError):
    def __init__(self, code: str, result: dict, *, after_verification: bool):
        super().__init__(code)
        self.detail = "阶段：" + ("提交安全验证之后" if after_verification else "首次提交账密")
        response_fields = sorted(result.keys() & _SDK_ERROR_FIELDS)
        diagnostics = ["响应字段：" + "、".join(response_fields)]
        invalid_field = ""
        for key in ("message", "server_message", "msg"):
            message = result.get(key)
            if not isinstance(message, str) or not message.strip():
                continue
            # 这种完整格式的第一个词是参数名，即使参数尚未列入本地字典，也不记录任何值。
            field = re.fullmatch(r"([a-zA-Z][a-zA-Z0-9_-]{0,27}) param (?:invalid|illegal)\.?", message.strip(), re.I)
            if field:
                invalid_field = field.group(1)
                diagnostics.append(key + "=" + invalid_field + " param invalid")
                continue
            # 保留已知错误词的顺序。每个未知片段都替换为省略号，避免保存账号、口令或令牌。
            words = (word.strip(".") for word in re.findall(r"[a-z0-9_/+@.=-]+|[\u3400-\u9fff]+", message[:240].lower()))
            filtered = [word if word in _SDK_ERROR_WORDS or word in {
                "参数错误", "请求错误", "无效请求", "请求参数错误", "验证码错误", "验证码失效", "验证失败"
            } else "…" for word in words]
            if filtered:
                diagnostics.append(key + "=" + " ".join(filtered))
        self.diagnostic = " | ".join(diagnostics)
        if invalid_field:
            self.detail += "。B 站拒绝的字段：" + invalid_field
            return
        # 响应可能含登录信息。只报告已知参数名及常见固定错误提示。
        for key in ("message", "server_message", "msg"):
            message = result.get(key)
            if not isinstance(message, str):
                continue
            text = message.strip().lower()
            fields = sorted(set(re.findall(r"\b[a-z][a-z_]+\b", text)) & _LOGIN_FIELDS)
            if fields:
                self.detail += "。B 站提示涉及 " + "、".join(fields)
                break
            if text in {"bad request", "request error", "request failed", "invalid request", "invalid parameter",
                        "request param illegal", "param illegal", "参数错误", "请求错误", "无效请求", "请求参数错误"}:
                self.detail += "。B 站提示 " + text
                break
        else:
            self.detail += "。B 站未提供可安全显示的详情"


def login_with_password(client: GameClient) -> Dict[str, str]:
    if client.server == "uo":
        raise ExportError("PASSWORD_SERVER_UNSUPPORTED")
    submitted = sys.stdin.buffer.readline(8193)
    if not submitted or len(submitted) > 8192:
        raise ExportError("PASSWORD_INPUT_INVALID")
    try:
        credentials = json.loads(submitted)
    except (ValueError, UnicodeError):
        raise ExportError("PASSWORD_INPUT_INVALID") from None
    del submitted
    if not isinstance(credentials, dict):
        raise ExportError("PASSWORD_INPUT_INVALID")
    account, password = credentials.pop("account", None), credentials.pop("password", None)
    if not isinstance(account, str) or not 1 <= len(account.strip()) <= 254:
        raise ExportError("PASSWORD_INPUT_INVALID")
    if not isinstance(password, str) or not password:
        raise ExportError("PASSWORD_INPUT_INVALID")

    emit("requesting_cipher")
    seed = client.request_sdk("/api/external/issue/cipher/v3", {"cipher_type": "bili_login_rsa"})
    if str(seed.get("code")) != "0":
        raise ExportError("PASSWORD_CIPHER_INIT_FAILED")
    public_key, salt = seed.get("cipher_key"), seed.get("hash")
    if not isinstance(public_key, str) or not isinstance(salt, str) or not salt:
        raise ExportError("PASSWORD_CIPHER_RESPONSE_INVALID")
    try:
        rsa = RSA.import_key(public_key)
        payload = (salt + password).encode("utf-8")
        if rsa.has_private():
            raise ExportError("PASSWORD_CIPHER_RESPONSE_INVALID")
        if len(payload) > rsa.size_in_bytes() - 11:
            raise ExportError("PASSWORD_INPUT_INVALID")
        ciphertext = PKCS1_v1_5.new(rsa).encrypt(payload)
        del payload
    except (ValueError, TypeError, IndexError):
        raise ExportError("PASSWORD_CIPHER_RESPONSE_INVALID") from None
    finally:
        del password
    # SDK 用 Base64 原值参与签名，由 HTTP 层统一进行一次表单编码。
    encoded = base64.b64encode(ciphertext).decode("ascii")
    params = {"user_id": account.strip(), "pwd": encoded}
    emit("submitting_password")
    result = client.request_sdk("/api/external/login/v3", params)
    code = str(result.get("code"))
    after_verification = code in _VERIFICATION_CODES or str(result.get("need_captch")) in {"1", "True"}
    if after_verification:
        emit("requesting_verification")
        params.update(verify_password_login(client, lambda url: emit("verification", url=url)))
        emit("checking_verification")
        result = client.request_sdk("/api/external/login/v3", params)
        code = str(result.get("code"))
        if code in _VERIFICATION_CODES or str(result.get("need_captch")) in {"1", "True"}:
            error_code = code if re.fullmatch(r"-?[0-9]{1,8}", code) else "UNKNOWN"
            raise PasswordLoginError("PASSWORD_VERIFICATION_REJECTED_" + error_code, result, after_verification=True)
    if code != "0":
        error_code = code if re.fullmatch(r"-?[0-9]{1,8}", code) else "UNKNOWN"
        raise PasswordLoginError("PASSWORD_LOGIN_REJECTED_" + error_code, result, after_verification=after_verification)
    uid, access_key = result.get("uid"), result.get("access_key")
    if not str(uid).isdigit() or not isinstance(access_key, str) or not access_key:
        raise ExportError("SDK_CREDENTIAL_FIELDS_MISSING")
    user = {"uid": str(uid), "access_key": access_key}
    for name in ("uname", "username"):
        if isinstance(result.get(name), str):
            user[name] = result[name]
    return user


def login_with_qr(client: GameClient) -> Dict[str, str]:
    import qrcode

    ticket, value = client.generate()
    image = io.BytesIO()
    qrcode.make(value).save(image, format="PNG")
    emit("qr", image=base64.b64encode(image.getvalue()).decode("ascii"))
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        phase, user = client.poll(ticket)
        if phase == "authorized":
            return user
        if phase == "expired":
            break
        emit(phase)
        time.sleep(2)
    raise ExportError("QR_EXPIRED_RESCAN_REQUIRED")


def execute(args) -> None:
    base = Path(__file__).resolve().parent
    public = (base / "import-public.pem").read_bytes()
    if args.self_test:
        user = dict(uid="10001", access_key="synthetic-self-test-only")
    else:
        if not re.fullmatch(r"[1-9][0-9]{4,19}", args.qq):
            raise ExportError("QQ_INVALID")
        filename = "game-protocol-uo.json" if args.server == "uo" else "game-protocol.json"
        profile = load_profile(base / filename)
        if profile is None:
            raise ExportError("LOCAL_PROFILE_REQUIRED")
        client = GameClient(profile, args.server)
        user = login_with_password(client) if args.password_login else login_with_qr(client)
    try:
        if not args.self_test:
            emit("checking_account")
            # 验证选中区服存在可登录的角色；响应只在内存中使用，不保存账号抓包。
            client.toplogin(dict(user), lambda phase: None, lambda: False)
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
    parser.add_argument("--password-login", action="store_true")
    try:
        execute(parser.parse_args())
    except PasswordLoginError as error:
        emit("error", code=error.code, detail=error.detail, diagnostic=error.diagnostic)
        sys.exit(1)
    except Exception as error:
        emit("error", code=error.code if isinstance(error, ExportError) else type(error).__name__)
        sys.exit(1)
