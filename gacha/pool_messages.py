"""国服卡池日程格式化与合并转发节点分段。"""
from typing import Any, Dict, List, Optional

NODE_TEXT_LIMIT = 1000
FORWARD_BODY_NODE_LIMIT = 4


def batch_forward_messages(messages: List[str], title: str = "FGO 国服卡池日程") -> List[List[str]]:
    """限制单张转发卡片的文本总量，保留节点顺序，并标出完整日程的分页。"""
    bodies = [messages[index:index + FORWARD_BODY_NODE_LIMIT]
              for index in range(0, len(messages), FORWARD_BODY_NODE_LIMIT)]
    return [[f"{title}（{index + 1}/{len(bodies)}）", *body]
            for index, body in enumerate(bodies)]


def build_nested_forward_nodes(
    messages: List[str], account_id: str, title: str = "FGO 国服卡池日程"
) -> List[Dict[str, Any]]:
    """每页生成一组内部转发节点，再作为外层转发的一条消息，最终只发送一张卡片。"""
    nodes: List[Dict[str, Any]] = []
    for page_index, batch in enumerate(batch_forward_messages(messages, title)):
        # SDK 仅归一化顶层 segments；嵌套节点须使用 Host 的 content/data 字段。
        children = [{"user_id": account_id, "user_nickname": title,
                     "message_id": f"fgo_page_{page_index}_node_{index}",
                     "content": [{"type": "text", "data": text}]}
                    for index, text in enumerate(batch)]
        nodes.append({"user_id": account_id, "nickname": title,
                      "segments": [{"type": "forward", "data": children}]})
    return nodes


def split_message_blocks(blocks: List[str], limit: int = NODE_TEXT_LIMIT) -> List[str]:
    """按卡池段落分段；极长子池列表再按行切分，确保每个节点不超出字数上限。"""
    if limit <= 0:
        raise ValueError("消息字数上限必须大于零")
    messages: List[str] = []
    current = ""
    for block in blocks:
        if len(block) <= limit:
            pieces = [block]
        else:
            pieces = []
            for line in block.splitlines():
                pieces.extend(line[index:index + limit] for index in range(0, len(line), limit))
        for piece in pieces:
            separator = "\n\n" if current else ""
            if len(current) + len(separator) + len(piece) > limit:
                messages.append(current)
                current = piece
            else:
                current += separator + piece
    if current:
        messages.append(current)
    return messages


def format_pool_messages(pools: List[dict], banner: Optional[dict], summary: str) -> List[str]:
    """按已排序的国服日程生成多条转发文本，并注明预测日期与不可用数据。"""
    header = ["FGO 国服卡池：当前 → 未来", summary]
    if banner is not None:
        header.append(f"当前选择：[{banner['id']}] {banner['title']}")
    header.extend([
        "切换：/fgo switch 卡池编号 [子池编号]；省略子池编号时选择首个子池",
        "未来日期为 Mooncell 预计，以国服实际公告为准。",
    ])
    blocks = ["\n".join(header)]
    for pool in pools:
        estimated = "（预计）" if pool["estimated"] else ""
        lines = [
            f"[{pool['id']}] [{pool['status']}] {pool['title']}",
            f"{pool['start_date']} ～ {pool['end_date']}{estimated}",
        ]
        if not pool["available"]:
            lines.append("模拟数据暂不可用，不能切换抽取")
        elif pool["type"] == "daily pickup":
            lines.extend(f"  子池 [{child['id']}] {child['sub_title']}" for child in pool["sub_pool"])
        blocks.append("\n".join(lines))
    return split_message_blocks(blocks)
