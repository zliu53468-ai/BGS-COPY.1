"""Flex contract: real probabilities, original candidate, no fabricated table/timer."""
import copy
import json
import unittest
from urllib.parse import parse_qs

import private_points_ui as ui


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


class PrivatePointsUITests(unittest.TestCase):
    def report(self):
        return {"probabilities": {"B": .46, "P": .45, "T": .09}}

    def test_result_uses_raw_probabilities_and_candidate_not_largest_probability(self):
        report = self.report()
        original = copy.deepcopy(report)
        panel = ui.result_panel(report, "P", 5, 61, "run-1")
        text = [n["text"] for n in nodes(panel) if n.get("type") == "text"]
        self.assertEqual(report, original)
        for value in ("第 61 局", "46.0%", "45.0%", "9.0%", "模型估計機率：45.0%", "模擬注數：5 注"):
            self.assertIn(value, text)
        for forbidden in ("桌號", "倒數", "最高比例", "觀望"):
            self.assertNotIn(forbidden, json.dumps(panel, ensure_ascii=False))
        self.assertEqual([n["width"] for n in nodes(panel) if "width" in n], ["46.0%", "45.0%", "9.0%"])
        actions = [parse_qs(n["data"]) for n in nodes(panel) if n.get("type") == "postback"]
        self.assertEqual([a["action"][0] for a in actions], ["points_continue", "points_end"])
        self.assertTrue(all(a["run"] == ["run-1"] for a in actions))

    def test_flex_payload_limits_and_supported_actions(self):
        panels = [ui.venue_panel([{"code": "DG", "name": "DG真人"}, {"code": "MT", "name": "MT真人"}], "flow"),
                  ui.game_panel("DG真人", "DG", "flow"), ui.input_panel("run"),
                  ui.result_panel(self.report(), "B", 20, 72, "run"), ui.ended_panel()]
        for panel in panels:
            self.assertEqual(panel["type"], "flex")
            self.assertTrue(0 < len(panel["altText"]) <= 400)
            self.assertLess(len(json.dumps(panel["contents"], ensure_ascii=False).encode()), 30000)
            self.assertEqual(panel["contents"]["header"]["backgroundColor"], "#101F19")
            for n in nodes(panel):
                if n.get("type") == "postback":
                    self.assertLessEqual(len(n["label"]), 20)
                    self.assertLessEqual(len(n["data"]), 300)
                if n.get("type") == "text":
                    self.assertTrue(n["text"])

    def test_game_menu_has_only_supported_baccarat_and_navigation(self):
        panel = ui.game_panel("DG真人", "DG", "flow")
        actions = [parse_qs(n["data"]) for n in nodes(panel) if n.get("type") == "postback"]
        self.assertEqual([a.get("game") for a in actions if "game" in a], [["baccarat"]])
        self.assertNotIn("龍虎", json.dumps(panel, ensure_ascii=False))

    def test_zero_and_full_probability_bars_stay_in_range(self):
        for prob in ({"B": 1., "P": 0., "T": 0.}, {"B": 0., "P": 1., "T": 0.}):
            panel = ui.result_panel({"probabilities": prob}, "B", 5, 2, "run")
            widths = [n["width"] for n in nodes(panel) if "width" in n]
            self.assertEqual(widths, ["100.0%"])

    def test_invalid_probabilities_cannot_be_rendered_as_confidence(self):
        for probabilities in ({}, {"B": .8, "P": .8, "T": .1},
                              {"B": float("nan"), "P": .4, "T": .1},
                              {"B": -.1, "P": 1., "T": .1}):
            with self.assertRaises(ValueError):
                ui.result_panel({"probabilities": probabilities}, "B", 5, 2, "run")
        with self.assertRaises(ValueError):
            ui.result_panel(self.report(), "SKIP", 0, 2, "run")


if __name__ == "__main__":
    unittest.main()
