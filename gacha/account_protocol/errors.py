"""异常只携带固定代码，不包含原始报文或凭据。"""

__all__ = ["ExportError", "MAINTENANCE_MESSAGE", "check_maintenance"]

MAINTENANCE_MESSAGE = "FGO 服务器正在维护中，请维护结束后再试。"


class ExportError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def check_maintenance(row: dict) -> None:
    """维护响应可能没有业务 nid，先检查服务端明确返回的动作标记。"""
    failure = row.get("fail")
    if isinstance(failure, dict) and failure.get("action") == "maintain":
        raise ExportError("SERVER_MAINTENANCE")
