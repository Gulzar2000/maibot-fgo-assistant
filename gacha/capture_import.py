"""离线导入原始 toplogin，按国服游戏 ID 隔离并保留导入来源。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import hashlib
import json

from .credentials import atomic_write, json_bytes
from .rate_chart import history_points
from .real_statistics import build_real_statistics, count, table


def game_history_key(root: dict) -> str:
    games = table(root["cache"], "userGame", "userId")
    if len(games) != 1:
        raise ValueError("抓包账号数量无效")
    return "fgo:CN:" + str(count(games[0], "userId"))


def merge_points(existing: List[Dict[str, Any]], imported: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按观测 ID 或来源去重，抽数相同的不同观测全部保留。"""
    def identity(point: Dict[str, Any]) -> str:
        if "snapshot_id" in point:
            return "capture:" + point["snapshot_id"]
        if "source_sha256" in point:
            return "file:" + point["source_sha256"]
        return "legacy:" + json.dumps(point, sort_keys=True, ensure_ascii=False)

    points = [dict(point) for point in existing]
    seen = {identity(point) for point in points}
    for point in imported:
        if identity(point) not in seen:
            points.append(dict(point))
            seen.add(identity(point))
    return sorted(points, key=lambda point: point.get("captured_at", point["day"] + "T23:59:59+08:00"))


def migrate_game_history(history: Dict[str, Dict[str, Any]]) -> None:
    """已核实归属的旧摘要转存到 FGO ID 条目；未识别旧数据保留原位。"""
    for key, entry in list(history.items()):
        game_key = entry.get("fgo_history_key")
        if key.startswith("fgo:") or not game_key:
            continue
        archive = history.setdefault(game_key, {"day": entry["day"]})
        points = merge_points(history_points(archive), history_points(entry))
        if points:
            archive["snapshots"] = points
            archive["summary"] = points[-1]
            entry.pop("snapshots", None)
            entry.pop("summary", None)
        archive["day"] = max(archive["day"], entry["day"])


def import_captures(
    directory: Path, metadata_path: Path, history_path: Path,
    bindings: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """先完整解析再写入；bindings 仅接受外部已核实的游戏历史键到绑定键映射。"""
    metadata = json.loads(metadata_path.read_bytes())
    original = history_path.read_bytes() if history_path.exists() else b"{}"
    history = json.loads(original)
    imported: Dict[str, List[Dict[str, Any]]] = {}
    for path in sorted(directory.glob("*.json")):
        raw = path.read_bytes()
        root = json.loads(raw)
        key = game_history_key(root)
        _, rate = build_real_statistics(root, metadata)
        if rate is None:
            raise ValueError("抓包无法计算出率：" + path.name)
        captured = datetime.fromtimestamp(count(root["cache"], "serverTime"), timezone(timedelta(hours=8)))
        imported.setdefault(key, []).append({
            "day": captured.date().isoformat(), "captured_at": captured.isoformat(),
            **rate._asdict(), "source_file": path.name,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
        })
    for key, points in imported.items():
        archive = history.setdefault(key, {"day": points[-1]["day"]})
        archive["snapshots"] = merge_points(history_points(archive), points)
        archive["summary"] = archive["snapshots"][-1]
        archive["day"] = max(archive["day"], archive["summary"]["day"])
        if bindings and key in bindings:
            entry = history[bindings[key]]
            entry["fgo_history_key"] = key
            entry["snapshots"] = merge_points(history_points(entry), archive["snapshots"])
            entry["summary"] = entry["snapshots"][-1]
    migrate_game_history(history)
    # 保存原文件的逐字节备份；重复导入不创建重复观测。
    if history_path.exists() and history_path.read_bytes() != original:
        raise RuntimeError("统计历史在导入期间发生变化，请重新导入")
    backup = history_path.with_name(history_path.name + ".before-import-" + hashlib.sha256(original).hexdigest()[:12] + ".bak")
    if not backup.exists():
        atomic_write(backup, original)
    atomic_write(history_path, json_bytes(history))
    return {"files": sum(len(points) for points in imported.values()), "accounts": len(imported),
            "account_points": {key: len(history[key]["snapshots"]) for key in imported}, "backup": str(backup)}
