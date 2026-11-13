"""异常携带固定代码及可公开的维护时间，不包含原始报文或凭据。"""

from datetime import datetime
from typing import Optional
import re

__all__ = ["ExportError", "MAINTENANCE_MESSAGE", "check_maintenance"]

MAINTENANCE_MESSAGE = "FGO 服务器正在维护中，请维护结束后再试。"


class ExportError(Exception):
    def __init__(self, code: str, *, maintenance_end: Optional[datetime] = None):
        self.code = code
        self.maintenance_end = maintenance_end
        super().__init__(code)

    @property
    def maintenance_message(self) -> str:
        if self.maintenance_end is None:
            return MAINTENANCE_MESSAGE
        return (
            MAINTENANCE_MESSAGE + "\n预计开服时间："
            + self.maintenance_end.strftime("%Y-%m-%d %H:%M") + "（北京时间，以实际开服为准）。"
        )


def check_maintenance(row: dict) -> None:
    """维护响应可能没有业务 nid，先检查服务端明确返回的动作标记。"""
    failure = row.get("fail")
    if isinstance(failure, dict) and failure.get("action") == "maintain":
        end = None
        detail = failure.get("detail")
        if isinstance(detail, str):
            # 服务端 detail 使用字面量 \\n；只提取明确标注的时间，不向用户透传原始报文。
            match = re.search(
                r"预计(?:开放|开服)时间[：:]\s*(?:公元)?(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[:：](\d{2})",
                detail.replace("\\n", "\n"),
            )
            if match is not None:
                try:
                    end = datetime(*(int(value) for value in match.groups()))
                except ValueError:
                    # 公告时间无效时仍应准确提示维护，不展示无效日期。
                    pass
        raise ExportError("SERVER_MAINTENANCE", maintenance_end=end)
