"""从者图鉴的正式持有判定。"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gacha.box import build_box_cards, owned_servants


def card(instance: int, servant: int, status: int | str | None, np_level: int = 1) -> dict:
    return {"id": instance, "svtId": servant, "status": status, "treasureDeviceLv1": np_level,
            "lv": 90, "limitCount": 4, "skillLv1": 10, "skillLv2": 10, "skillLv3": 10}


class BoxOwnershipTests(unittest.TestCase):
    def test_excludes_withdrawn_but_keeps_owned_cards_with_other_flags(self):
        inventory = [card(1, 500300, "22"), card(2, 100100, 17), card(3, 800100, 144)]
        storage = [card(4, 100200, 5), card(5, 100300, None), card(6, 100500, "16")]
        cache = {"replaced": {"userSvt": inventory, "userSvtStorage": storage}}
        self.assertEqual({row["id"] for row in owned_servants({"cache": cache})}, {2, 3, 5, 6})

    def test_permanent_copy_survives_filter_before_duplicate_selection(self):
        withdrawn = card(1, 500300, 22, np_level=5)
        permanent = card(2, 500300, 16)
        cache = {"replaced": {"userSvt": [withdrawn], "userSvtStorage": [permanent]}}
        index = {500300: {"type": "normal", "collectionNo": 62, "classId": 5}}
        cards = build_box_cards(cache, owned_servants({"cache": cache}), index)
        self.assertEqual([(entry.number, entry.np_level) for entry in cards], [(62, 1)])
