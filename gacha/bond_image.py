"""将羁绊矿量与上次查询变化绘制为一张 QQ 报告图。"""
from io import BytesIO
from typing import List
from PIL import Image, ImageDraw, ImageFont
import base64

from .image_gen import _find_font


def render_bond_report(report: dict, res_dir: str) -> str:
    fonts = {size: ImageFont.truetype(_find_font(res_dir), size) for size in (23, 27, 32, 45, 82)}
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines = []
    y = 40

    def text(value: str, size: int = 27, color: str = "#20334C") -> None:
        nonlocal y
        line = ""
        for char in value:
            if char == "\n" or (line and measure.textlength(line + char, font=fonts[size]) > 930):
                lines.append((48, y, line, size, color))
                y += size + 14
                line = "" if char == "\n" else char
            else:
                line += char
        if line:
            lines.append((48, y, line, size, color))
            y += size + 14

    def section(title: str) -> None:
        nonlocal y
        y += 24
        text(title, 32, "#087F8C")
        y += 6

    text("FGO · 羁绊计算 / 挖矿", 45)
    text(str(report["name"]), 27)
    text("国服 · " + report["captured_at"].replace("T", " ")[:19], 23, "#63738A")
    section("当前已解锁、尚未获得的圣晶石")
    text(f"{report['remaining']:,} 石", 82, "#087F8C")
    text(f"普通羁绊 {report['normal']:,} 石 ＋ 已喂梦火待兑现 {report['dream']:,} 石")
    section("梦火 · 已喂与背包库存")
    text(f"已喂、奖励未获得：{report['dreamfire_pending']} 个 / {report['dream']:,} 石", 32, "#087F8C")
    text("继续刷到已解锁的羁绊上限即可获得，已计入上方总数。", 23, "#63738A")
    text(f"背包未使用梦火：{report['dreamfire_inventory']} 个", 32, "#A36615")
    text(f"其中可用于现有从者 {report['dreamfire_usable']} 个，可再解锁 {report['inventory_quartz']:,} 石。")
    text("需要先喂梦火，再刷对应羁绊；未计入上方已解锁总数。", 23, "#63738A")
    text(f"含背包梦火的可挖潜力：{report['potential']:,} 石", 32, "#A36615")
    text(f"已喂且奖励已获得：{report['dreamfire_rewarded']} 个 / {report['dreamfire_rewarded'] * 30:,} 石", 23, "#63738A")
    text(f"已喂合计 {report['dreamfire_used']} 个    计入 {len(report['servants'])} 名从者", 23, "#63738A")
    text(f"累计等级奖励 {report['earned']:,} 石（含普通羁绊与已兑现梦火）", 23, "#63738A")
    section("01  按星级汇总")
    for rarity in range(5, -1, -1):
        rows = [row for row in report["servants"] if row["rarity"] == rarity]
        if rows:
            normal = sum(row["normal"] for row in rows)
            dream = sum(row["dream"] for row in rows)
            text(f"{rarity}★  {len(rows)} 人    普通 {normal} / 已喂待兑现 {dream} / 合计 {normal + dream} 石")
    section("02  剩余奖励最多 · 前 10 名")
    top = sorted((row for row in report["servants"] if row["remaining"]),
                 key=lambda row: (-row["remaining"], -row["rarity"], row["name"]))[:10]
    if not top:
        text("当前上限内的羁绊圣晶石奖励已全部获得。")
    for index, row in enumerate(top, 1):
        text(f"{index:02}  {row['name']}  {row['rank']}/{row['cap']} 绊  +{row['remaining']} 石")
    section("03  已喂梦火 · 奖励尚未获得")
    dream_rows: List[dict] = sorted((row for row in report["servants"] if row["dream"]),
                                    key=lambda row: (-row["dream"], row["next"], row["name"]))
    if not dream_rows:
        text("暂无已解锁且尚未获得的梦火段奖励。")
    for row in dream_rows:
        text(f"{row['name']}  {row['rank']}/{row['cap']} 绊  待兑现 {row['dream'] // 30} 个 / {row['dream']} 石")
        text(f"距下一级 {row['next']:,} 经验", 23, "#63738A")
    section("04  与上次查询对比")
    comparison = report["comparison"]
    if comparison is None:
        text("首次查询，已建立羁绊快照；下次可查看变化。")
    else:
        text("上次：" + comparison["since"].replace("T", " ")[:19], 23, "#63738A")
        text(f"原有从者升级奖励 +{comparison['gained']} 石；梦火新增解锁 +{comparison['unlocked']} 石")
        text(f"新增从者 {comparison['added']} 名；剩余矿量变化 {comparison['delta']:+} 石")
        if comparison["missing"]:
            text(f"上次有 {comparison['missing']} 名从者本次未出现，未推算其收益。", 23, "#A36615")
        for change in comparison["changes"]:
            text(change, 23)
        if not comparison["changes"]:
            text("从者羁绊等级及上限没有变化。", 23, "#63738A")
    section("计算口径")
    text("普通段按国服星级奖励；安哥拉曼纽 6～9 绊每级 2 石；11～15 绊每级 30 石。", 23, "#63738A")
    text("同图鉴共享形态去重；已契约图鉴包含曾获得的从者，不按持有卡片张数重复计数。", 23, "#63738A")
    text("玛修剧情羁绊、礼装及不可用图鉴不计入。累计奖励按当前等级推算，并非圣晶石余额或领取流水。", 23, "#63738A")
    text("国服从者与经验：Atlas Academy；奖励核对：Mooncell。", 23, "#63738A")
    text("功能参考：okita27 / FGO-bond-quartz", 23, "#63738A")
    canvas = Image.new("RGB", (1024, y + 42), "#F3F6FA")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, 12, canvas.height), fill="#087F8C")
    for x, top_y, value, size, color in lines:
        draw.text((x, top_y), value, font=fonts[size], fill=color)
    output = BytesIO()
    canvas.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")
