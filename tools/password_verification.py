"""在绑定工具内显示 B 站官方验证页面，接收用户手动完成的挑战。"""
from queue import Empty, Queue
from threading import Thread
from typing import Callable, Dict
from urllib.parse import urlencode
import json
import re
import sys

from account_protocol.errors import ExportError
from account_protocol.protocol import GameClient


def verify_password_login(client: GameClient, open_page: Callable[[str], None]) -> Dict[str, str]:
    challenge = client.request_sdk("/api/external/start_captcha/v3")
    if str(challenge.get("code")) != "0" or challenge.get("captcha_type") != "geetest":
        raise ExportError("PASSWORD_VERIFICATION_UNAVAILABLE")
    gt, initial, ctoken = (challenge.get(name) for name in ("gt", "challenge", "ctoken"))
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value)
           for value in (gt, initial, ctoken)):
        raise ExportError("PASSWORD_VERIFICATION_RESPONSE_INVALID")

    query = urlencode({"code": "0", "captcha_type": "1", "gt": gt, "challenge": initial})
    open_page("https://game.bilibili.com/sdk/geetest_v2/?" + query)

    # 窗口在同一个匿名管道上返回官方页面的结果。ctoken 只保留在这个进程内，用于绑定服务端挑战。
    inbox: Queue[bytes] = Queue(maxsize=1)
    Thread(target=lambda: inbox.put(sys.stdin.buffer.raw.readline(4097)), daemon=True).start()
    try:
        submitted = inbox.get(timeout=120)
    except Empty:
        raise ExportError("PASSWORD_VERIFICATION_EXPIRED") from None
    if not submitted or len(submitted) > 4096:
        raise ExportError("PASSWORD_VERIFICATION_CANCELLED")
    try:
        answer = json.loads(submitted)
    except (ValueError, UnicodeError):
        raise ExportError("PASSWORD_VERIFICATION_RESPONSE_INVALID") from None
    if not isinstance(answer, dict) or answer.get("status") != 1 or answer.get("captcha_type") != 1:
        raise ExportError("PASSWORD_VERIFICATION_CANCELLED")
    fields = {name: answer.get(name) for name in ("challenge", "validate", "seccode")}
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.|+-]{1,256}", value)
           for value in fields.values()):
        raise ExportError("PASSWORD_VERIFICATION_RESPONSE_INVALID")
    # H5 回调中的数字 1 只用于界面。安卓 SDK 提交时会恢复挑战接口返回的类型。
    return {"captcha_type": challenge["captcha_type"], "ctoken": ctoken, **fields}
