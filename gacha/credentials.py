"""Windows CurrentUser DPAPI storage; adapted from FGO-Capture-Kit local_qr.py."""
from ctypes import wintypes
from pathlib import Path
import ctypes
import json
import os
import uuid

from .account_protocol.errors import ExportError


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def protect(raw: bytes, decrypt: bool = False) -> bytes:
    if os.name != 'nt':
        raise ExportError('WINDOWS_REQUIRED')
    buf = ctypes.create_string_buffer(raw)
    src = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    salt_buf = ctypes.create_string_buffer(b'FGO-Capture-Kit.local-qr.credentials.v1')
    salt = Blob(len(salt_buf.raw) - 1, ctypes.cast(salt_buf, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(src), None, ctypes.byref(salt), None, None, 1, ctypes.byref(out)):
        raise ExportError('LOCAL_CREDENTIAL_PROTECTION_FAILED')
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.data, ctypes.c_void_p))


def atomic_write(path: Path, raw: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(raw)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def validate_credentials(user):
    if not isinstance(user, dict) or not str(user.get('uid', '')).isdigit():
        raise ExportError('LOCAL_CREDENTIAL_INVALID')
    if not isinstance(user.get('access_key'), str) or not user['access_key']:
        raise ExportError('LOCAL_CREDENTIAL_INVALID')
