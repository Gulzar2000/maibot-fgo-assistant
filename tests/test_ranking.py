"""用合成快照验证群排行榜，无需登录游戏或发送 QQ 消息。"""
from io import BytesIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from PIL import Image
import base64
import json
import tempfile
import unittest

from test_pool_query import plugin
from gacha.real_statistics import CaptureRate


class RankingTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_uses_local_snapshots_and_compares_per_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance = plugin.FgoGachaPlugin()
            members = [
                {"user_id": user_id, "card": "超长群昵称" * 40 if user_id == 101 else " ",
                 "nickname": "QQ昵称"}
                for user_id in range(101, 107)
            ]
            api = AsyncMock(return_value=members)
            send = SimpleNamespace(image=AsyncMock(return_value=True), text=AsyncMock(), hybrid=AsyncMock())
            instance._ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=directory),
                                            api=SimpleNamespace(call=api), send=send)
            store = instance.accounts
            history_path = root / "fgogacha" / "capture_history.json"
            history = {}
            with patch(f"{plugin.AccountStore.__module__}.protect", side_effect=lambda raw, decrypt=False: raw), \
                    patch.object(plugin.AccountStore, "client", side_effect=AssertionError("不得启动游戏登录")), \
                    patch.object(plugin.AccountStore, "_import_locked", side_effect=AssertionError("不得导入绑定")) as importer, \
                    patch.object(plugin, "render_ranking", wraps=plugin.render_ranking) as render, \
                    patch.object(plugin, "fetch_avatars", new_callable=AsyncMock) as avatars:
                with BytesIO() as image_bytes:
                    Image.new("RGB", (20, 20), "blue").save(image_bytes, "PNG")
                    avatars.return_value = [image_bytes.getvalue()] * 5
                for user_id, ssr in enumerate((12, 11, 10, 9, 8, 99, 99), 101):
                    user = {"uid": str(user_id + 100), "access_key": "synthetic"}
                    store.save(str(user_id), "android", user, Event())
                    binding_key = store._capture_key("android", user)
                    game_key = f"fgo:CN:{user['uid']}"
                    history[binding_key] = {"day": "2026-11-25", "fgo_history_key": game_key}
                    old = {"day": "2026-11-24", "total_draws": 1000, "ssr_count": 100}
                    latest = {"day": "2026-11-25", "total_draws": 499 if user_id == 106 else 1000,
                              "ssr_count": ssr}
                    history[game_key] = {"day": latest["day"], "snapshots": [old, latest]}
                history_path.write_text(json.dumps(history), encoding="utf-8")

                async def command(group_id="123"):
                    return await instance.cmd_rank(stream_id="stream", group_id=group_id, platform="qq")

                self.assertEqual(await command(), (True, "", True))
                rows = render.call_args.args[0]
                self.assertEqual([row.user_id for row in rows], ["101", "102", "103", "104", "105"])
                self.assertEqual([row.change for row in rows], [None] * 5)
                self.assertEqual(rows[0].rate, CaptureRate(1000, 12, False))
                self.assertEqual(rows[1].nickname, "QQ昵称")
                self.assertEqual(rows[0].nickname, "超长群昵称" * 40)
                api.assert_awaited_once_with(
                    "adapter.napcat.group.get_group_member_list", version="1", group_id="123", no_cache=True,
                )
                send.image.assert_awaited_once()
                with Image.open(BytesIO(base64.b64decode(send.image.call_args.args[0]))) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.getpixel((120, 142)), (0, 0, 255))

                history["fgo:CN:202"]["snapshots"][-1]["ssr_count"] = 13
                history["fgo:CN:206"]["snapshots"][-1].update(total_draws=500, ssr_count=5)
                history_path.write_text(json.dumps(history), encoding="utf-8")
                send.image.return_value = False
                with self.assertLogs(plugin.logger, level="ERROR"):
                    await command()
                send.image.return_value = True
                self.assertEqual(await command(), (True, "", True))
                rows = render.call_args.args[0]
                self.assertEqual([row.user_id for row in rows], ["102", "101", "103", "106", "104"])
                self.assertEqual([row.change for row in rows], [1, -1, 0, None, -1])
                self.assertEqual(await command("456"), (True, "", True))
                self.assertEqual([row.change for row in render.call_args.args[0]], [None] * 5)
                importer.assert_not_called()
                send.text.assert_not_awaited()
                send.hybrid.assert_not_awaited()
