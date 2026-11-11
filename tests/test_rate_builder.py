from pathlib import Path
from unittest.mock import patch
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gacha.engine import _build_pool_data, _roll_once, simulate_ten_pull
from gacha.rate_builder import _roster, build_ce_only, build_pickup


def summon(svt: str, rates: str, ce: str = "", ce_rates: str = "") -> str:
    return ("|开始时间jp=2025-09-24 18:00\n{{推荐召唤情况\n"
            f"|从者序号=\n*{svt}\n|从者概率=\n*{rates}\n"
            f"|礼装序号=\n*{ce}\n|礼装概率=\n*{ce_rates}\n}}}}")


class RateBuilderTests(unittest.TestCase):
    def setUp(self):
        self.cards = {"svt": {"453": {"name": "彦斋", "rarity": 5}, "456": {"name": "近藤", "rarity": 5},
                              "454": {"name": "藤堂", "rarity": 4}, "210": {"name": "以藏", "rarity": 3}},
                      "ce": {"2459": {"name": "京乱", "rarity": 5}, "2460": {"name": "萤火", "rarity": 4},
                             "2461": {"name": "新选组", "rarity": 3}}}

    def test_single_up_has_exact_individual_probability_and_total(self):
        data = build_pickup(summon("453,454", "0.8%,2.1%", "2459,2460,2461", "2.8%,4.0%,8.0%"), "测试", self.cards)[0]
        rows = _build_pool_data(data)
        self.assertAlmostEqual(sum(r["weight"] for r in rows), 100)
        self.assertAlmostEqual(next(r["weight"] for r in rows if r["ids"] == ["453"]), 0.8)
        self.assertAlmostEqual(sum(r["weight"] for r in rows if r["type"] == "svt" and r["star"] == 5 and not r["pickup"]), 0.2)

    def test_double_up_is_per_card_not_per_group(self):
        data = build_pickup(summon("453,456", "各0.4%"), "双UP", self.cards)[0]
        row = next(r for r in data["servants"] if r["display"])
        self.assertEqual(row["ids"], ["453", "456"])
        self.assertAlmostEqual(row["weight"] / len(row["ids"]), 0.4)

    def test_missing_four_star_column_does_not_shift_three_star_rate(self):
        data = build_pickup(summon("453,210", "0.8%,4.0%", "2460", "4.0%"), "紧凑表", self.cards)[0]
        self.assertAlmostEqual(next(r["weight"] for r in data["servants"] if r["ids"] == ["210"]), 4)
        self.assertAlmostEqual(next(r["weight"] for r in data["crafts"] if r["display"]), 4)

    def test_overfull_probability_exposes_error(self):
        with self.assertRaises(ValueError):
            build_pickup(summon("453,456", "各0.8%"), "错误表", self.cards)

    def test_cn_probability_overrides_jp(self):
        raw = summon("453", "0.8%").replace("}}", "|从者概率cn=\n*0.7%\n}}")
        data = build_pickup(raw, "国服表", self.cards)[0]
        self.assertAlmostEqual(next(r["weight"] for r in data["servants"] if r["display"]), 0.7)

    def test_roster_rotation_and_future_servant_release(self):
        before, after = _roster("2025-10-07"), _roster("2025-10-08")
        self.assertIn("774", before["ce4"])
        self.assertNotIn("774", after["ce4"])
        self.assertIn("2470", after["ce4"])
        self.assertNotIn("463", _roster("2026-01-22")["svt4"])
        self.assertIn("463", _roster("2026-01-23")["svt4"])
        self.assertNotIn("913", _roster("2026-06-03")["ce4"])
        self.assertNotIn("2225", _roster("2026-06-03")["ce3"])

    def test_ce_only_pool_uses_restricted_servants_and_correct_guarantee(self):
        title = "【每日替换】「OVER THE SAME SKY-OCTOBER-」推荐召唤"
        resource = json.loads((Path(__file__).resolve().parents[1] / "res/ce_only_pools.json").read_text(encoding="utf8"))[title]
        cards = {"svt": {str(x): {"rarity": 5 if i < 9 else (4 if i < 11 else 3)}
                          for i, x in enumerate(resource["servants"])}, "ce": {}}
        children = build_ce_only("|推荐召唤从者=指定名单\n|推荐召唤礼装=英灵星行\n", title, cards)
        self.assertEqual(len(children), 3)
        data = children[0]
        self.assertTrue(all(not r["display"] for r in data["servants"]))
        self.assertEqual(data["crafts"][0]["weight"], 56)
        rows = _build_pool_data(data)
        with patch("gacha.engine.random.choice", side_effect=lambda ids: ids[0]):
            self.assertEqual(_roll_once(0.009, rows, data["golden_rates"])[0], "svt")
            self.assertEqual(_roll_once(0.041, rows, data["golden_rates"])[0], "cft")
        for _ in range(10):
            self.assertEqual(len(simulate_ten_pull(data)), 11)


if __name__ == "__main__":
    unittest.main()
