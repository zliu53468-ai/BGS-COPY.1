"""Fail-closed, isolated LINE group research text mode regression tests."""
from __future__ import annotations

import json
import copy
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from group_text_research import (
    VERSION, _kelly_simulation, handle_group_event,
)


class GroupTextResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "group.json"
        self.env = {
            "GROUP_TEXT_ENABLED": "1",
            "GROUP_ADMIN_UIDS": "U-admin-1,U-admin-2",
            "GROUP_ALLOWED_IDS": "C-group-1,C-group-2",
            "GROUP_STOP_LOSS_UNITS": "30",
        }
        self.number = 0
        self.histories = []

    def event(self, text, *, uid="U-admin-1", group="C-group-1",
              event_id=None, kind="text", source_type="group"):
        self.number += 1
        return {
            "webhookEventId": event_id or f"evt-{self.number}",
            "type": "message",
            "source": {
                "type": source_type,
                "groupId": group,
                "userId": uid,
            },
            "message": {"type": kind, "id": f"msg-{self.number}",
                        "text": text},
        }

    def predictor(self, history):
        self.histories.append(list(history))
        return {
            "version": VERSION, "action": "Skip",
            "validated_edge": False, "bankroll_fraction": 0,
            "probabilities": {"B": .66, "P": .32, "T": .02},
            "research_view": {
                "eligible": True, "direction": "B",
                "road_confirmed": True, "road_conflict": False,
            },
            "road_aux": {"estimated_only": True,
                         "validated_betting_edge": False},
        }

    def call(self, text, **kwargs):
        return handle_group_event(self.event(text, **kwargs),
                                  environ=self.env,
                                  state_path=self.path,
                                  predictor=self.predictor)

    def state(self, group="C-group-1"):
        return json.loads(self.path.read_text(encoding="utf-8"))["groups"][group]

    def test_uid_and_group_both_required(self):
        self.assertIsNone(self.call("開始分析", uid="U-outsider"))
        self.assertIsNone(self.call("開始分析", group="C-outsider"))
        self.assertIsNone(self.call("開始分析", uid=""))
        self.assertIsNone(self.call("開始分析", source_type="user"))
        self.assertIsNone(self.call("開始分析", kind="image"))
        self.assertFalse(self.path.exists())
        # Allowlisted UID can discover a new group's ID BEFORE group approval.
        msg = self.call("查詢群組ID", group="C-not-yet-approved")
        self.assertIn("C-not-yet-approved", msg)
        self.assertFalse(self.path.exists())
        self.env["GROUP_TEXT_ENABLED"] = "0"
        self.assertIsNone(self.call("開始分析"))
        self.assertFalse(self.path.exists())

    def test_single_admin_uid_env_alias_and_same_group_only(self):
        self.env["GROUP_ADMIN_UIDS"] = ""
        self.env["GROUP_ADMIN_UID"] = "U-admin-1"
        self.assertIsNone(self.call("開始分析", uid="U-admin-2"))
        self.assertIsNone(self.call("開始分析", group="C-unapproved"))
        started = self.call("開始分析", uid="U-admin-1")
        self.assertIn("已開始", started)
        message = self.call("紅")
        self.assertIn("第 2 局", message)
        self.assertIn("推薦方向：🔴 莊", message)
        self.assertIn("建議注數：5 注（模擬）", message)
        self.assertEqual(len(message.splitlines()), 3)
        self.assertEqual(self.state()["net_units"], 0)

    def test_start_outcomes_and_tie_are_group_only_and_skip(self):
        self.assertIn("已開始", self.call("開始分析"))
        msg = self.call("紅")
        self.assertIn("第 2 局", msg)
        self.assertIn("推薦方向：🔴 莊", msg)
        self.assertIn("建議注數：5 注（模擬）", msg)
        self.assertEqual(len(msg.splitlines()), 3)
        self.assertIn("第 3 局", self.call("藍"))
        self.assertIn("第 4 局", self.call("和"))
        self.assertEqual(self.state()["history"], ["B", "P", "T"])
        self.assertEqual(self.state()["net_units"], 0)
        self.assertEqual(self.histories, [
            ["B"], ["B", "P"], ["B", "P", "T"]])
        self.assertNotIn("bankroll", self.state())

    def test_ledger_stoploss_pause_and_new_session(self):
        self.call("開始分析")
        self.call("紅")
        self.assertIn("-10", self.call("-10"))
        self.assertIn("-20", self.call("-10"))
        pause = self.call("-10")
        self.assertIn("-30", pause)
        self.assertIn("暫停休息", pause)
        self.assertTrue(self.state()["paused"])
        self.assertIn("休息", self.call("藍"))
        self.assertEqual(self.state()["history"], ["B"])
        self.assertIn("累計：-30 注", self.call("查詢狀態"))
        self.assertIn("已開始", self.call("開始分析"))
        self.assertEqual(self.state()["net_units"], 0)
        self.assertFalse(self.state()["paused"])
        self.assertEqual(self.state()["history"], [])

    def test_repeated_event_id_cannot_double_apply(self):
        self.call("開始分析")
        event = self.event("-20", event_id="same-123")
        first = handle_group_event(
            event, environ=self.env,
            predictor=self.predictor, state_path=self.path)
        self.assertIn("-20", first)
        again = handle_group_event(
            event, environ=self.env,
            predictor=self.predictor, state_path=self.path)
        self.assertIsNone(again)
        self.assertEqual(self.state()["net_units"], -20)
        self.assertEqual(len(self.state()["records"]), 1)

    def test_admin_manual_settlement_and_previous_tier(self):
        self.call("開始分析")
        self.call("紅")
        self.assertEqual(self.state()["pending_tier"], 5)
        self.assertIn("-5", self.call("-"))
        self.assertEqual(self.state()["pending_tier"], 0)
        self.assertIn("沒有待結算", self.call("+"))
        self.call("藍")
        self.assertIn("+5", self.call("+"))
        self.assertEqual(self.state()["net_units"], 0)
        self.assertEqual(
            [x["delta_units"] for x in self.state()["records"]],
            [-5, 5],
        )

    def test_reject_invalid_and_fail_closed_on_wager(self):
        self.call("開始分析")
        self.assertIsNone(self.call("隨便聊天"))
        self.assertEqual(self.state()["history"], [])

        def unsafe(history):
            result = self.predictor(history)
            result["action"] = "B"
            return result

        with self.assertRaisesRegex(RuntimeError, "安全鎖定"):
            handle_group_event(self.event("紅"), environ=self.env,
                               predictor=unsafe, state_path=self.path)
        self.assertEqual(self.state()["history"], [])
        self.assertEqual(self.state()["net_units"], 0)

    def test_research_b_direction_displays_when_eligible_false(self):
        # Match private Soft Shoe display: direction B/P is independent of
        # research eligible, while the official betting gate remains locked.
        def b_direction(history):
            report = self.predictor(history)
            report["research_view"]["eligible"] = False
            return report

        self.call("開始分析")
        result = handle_group_event(
            self.event("紅"), environ=self.env,
            predictor=b_direction, state_path=self.path)
        self.assertIn("推薦方向：🔴 莊", result)
        self.assertIn("建議注數：5 注（模擬）", result)
        self.assertEqual(self.state()["last_research_direction"], "B")

    def test_research_skip_still_observes_when_not_eligible(self):
        def skip_direction(history):
            report = self.predictor(history)
            report["research_view"]["eligible"] = False
            report["research_view"]["direction"] = "SKIP"
            return report

        self.call("開始分析")
        result = handle_group_event(
            self.event("紅"), environ=self.env,
            predictor=skip_direction, state_path=self.path)
        self.assertIn("推薦方向：⚪ 觀望", result)
        self.assertIn("建議注數：0 注（模擬）", result)
        self.assertEqual(self.state()["last_research_direction"], "SKIP")

    def test_kelly_calculation_floored_to_tiers(self):
        for probs, tier in (
            ({"B": .5, "P": .49, "T": .01}, 0),
            ({"B": .66, "P": .32, "T": .02}, 5),
            ({"B": .80, "P": .19, "T": .01}, 10),
            ({"B": .96, "P": .03, "T": .01}, 20),
        ):
            with self.subTest(tier=tier):
                amount, fraction = _kelly_simulation(
                    {"probabilities": probs}, "B")
                self.assertEqual(amount, tier)
                self.assertLessEqual(amount, fraction * 100 + 1e-6)
        # No information to claim an actual advantage.
        self.assertEqual(_kelly_simulation({}, "SKIP"), (0, 0))

    def test_group_isolation_and_end_session(self):
        self.call("開始分析")
        self.call("紅")
        self.call("-10")
        self.call("開始分析", group="C-group-2")
        self.call("藍", group="C-group-2")
        self.assertEqual(self.state()["net_units"], -10)
        self.assertEqual(self.state("C-group-2")["net_units"], 0)
        self.assertEqual(self.state()["history"], ["B"])
        self.assertEqual(self.state("C-group-2")["history"], ["P"])
        self.assertIn("牌路、模擬注數", self.call("結束分析"))
        self.assertFalse(self.state()["active"])
        self.assertIn("開始分析", self.call("紅"))
        self.assertEqual(self.state()["history"], [])
        self.assertEqual(self.state()["net_units"], 0)
        self.assertEqual(self.state()["records"], [])
        # Another LINE group is unaffected by this group's reset.
        self.assertEqual(self.state("C-group-2")["history"], ["P"])

    def test_end_clears_road_ledger_tier_pause_and_rotates_run_id(self):
        self.call("開始分析")
        before = self.state()["run_id"]
        self.call("紅")
        self.call("-20")
        self.call("休息")
        state = self.state()
        self.assertEqual(state["history"], ["B"])
        self.assertTrue(state["paused"])
        self.assertEqual(state["net_units"], -20)
        self.assertEqual(len(state["records"]), 1)
        message = self.call("結束分析")
        self.assertIn("已清空", message)
        state = self.state()
        self.assertFalse(state["active"])
        self.assertFalse(state["paused"])
        self.assertEqual(state["history"], [])
        self.assertEqual(state["net_units"], 0)
        self.assertEqual(state["pending_tier"], 0)
        self.assertEqual(state["records"], [])
        self.assertEqual(state["last_research_direction"], "SKIP")
        self.assertNotEqual(state["run_id"], before)
        self.assertIn("已記錄牌局：0 局", self.call("查詢狀態"))
        self.assertIn("開始分析", self.call("紅"))
        self.call("開始分析")
        self.assertEqual(self.state()["history"], [])
        self.assertTrue(self.state()["active"])

    def test_bp_direction_gets_explicit_fixed_minimum_when_kelly_is_zero(self):
        def low_tier(hist):
            report = self.predictor(hist)
            report["probabilities"] = {"B": .51, "P": .48, "T": .01}
            return report
        self.call("開始分析")
        msg = handle_group_event(
            self.event("紅"), environ=self.env,
            predictor=low_tier, state_path=self.path)
        self.assertIn("推薦方向：🔴 莊", msg)
        self.assertNotIn("推薦方向：⚪ 觀望", msg)
        self.assertIn("建議注數：5 注（模擬，固定最低級距）", msg)
        self.assertEqual(len(msg.splitlines()), 3)
        self.assertEqual(self.state()["last_research_direction"], "B")
        self.assertEqual(self.state()["pending_tier"], 5)
        self.assertEqual(self.state()["last_simulation_tier"], 5)
        self.assertIn("上次模擬注數：5 注", self.call("查詢狀態"))
        self.assertIn("+5", self.call("+"))
        self.assertIn("沒有待結算", self.call("+"))

    def test_manual_result_aliases_work_without_any_screenshot(self):
        self.call("開始分析")
        commands = ("莊", "閒", "和", "b", "Ｐ", " t ", "🔴 莊", "🔵閒", "🟢 和")
        for index, command in enumerate(commands):
            result = self.call(command)
            self.assertIn(f"第 {index + 2} 局", result)
            self.assertIn("建議注數：", result)
            self.assertEqual(len(result.splitlines()), 3)
        self.assertEqual(self.state()["history"], list("BPTBPTBPT"))

    def test_fixed_minimum_cannot_mask_invalid_model_probabilities(self):
        self.call("開始分析")
        for probabilities in ({}, {"B": float("nan"), "P": .4},
                              {"B": .8, "P": .7}, {"B": 0, "P": 0}):
            report = self.predictor([])
            report["probabilities"] = probabilities
            with self.subTest(probabilities=probabilities), \
                    self.assertRaisesRegex(RuntimeError, "模型機率資料無效"):
                handle_group_event(self.event("莊"), environ=self.env,
                                   state_path=self.path, predictor=lambda _: report)
            self.assertEqual(self.state()["history"], [])
            self.assertEqual(self.state()["pending_tier"], 0)

    def test_text_only_real_engine_explains_early_observe_then_shows_units(self):
        self.call("開始分析")
        for number in range(1, 4):
            result = handle_group_event(self.event("莊"), environ=self.env,
                                        state_path=self.path)
            self.assertIn(f"第 {number + 1} 局", result)
            self.assertEqual(len(result.splitlines()), 3)
            if number < 3:
                self.assertIn("樣本不足，至少需 3 局", result)
                self.assertIn("建議注數：0 注（模擬）", result)
            else:
                self.assertIn("推薦方向：🔵 閒", result)
                self.assertIn("建議注數：5 注（模擬，固定最低級距）", result)
        self.assertEqual(self.state()["history"], list("BBB"))

    def test_actual_research_skip_keeps_skip_and_explains_causes(self):
        def uncertain(hist):
            report = self.predictor(hist)
            report["research_view"].update({
                "direction": "SKIP",
                "eligible": False,
                "candidate": "B",
                "reasons": ["weak_ev_separation", "low_gap_to_noise"],
            })
            return report
        self.call("開始分析")
        msg = handle_group_event(
            self.event("紅"), environ=self.env,
            predictor=uncertain, state_path=self.path)
        self.assertIn("推薦方向：⚪ 觀望", msg)
        self.assertIn("EV 差距不足", msg)
        self.assertEqual(len(msg.splitlines()), 3)
        self.assertIn("建議注數：0 注（模擬）", msg)
        self.assertEqual(self.state()["last_research_direction"], "SKIP")

    def test_real_bgs_adapter_research_group_direction_parity(self):
        """Group must present the exact 58a24c5 BGS research direction."""
        from bbb_soft_shoe_runtime import analyze_history, predict_screen
        from group_text_research import _research

        samples = [
            ["B", "P", "T", "B", "P", "P", "B", "T", "B", "P", "B", "B"],
            list("BBPPPTTBBPBPTPPBBTP"),
        ]
        for history in samples:
            with self.subTest(history_len=len(history)):
                report = analyze_history(history)
                adapter = predict_screen(history)
                direction, tier, message = _research(history, lambda _: report)
                self.assertEqual(direction, adapter["bbb_soft_shoe"]["display_direction"])
                self.assertEqual(direction, adapter["research_view"]["direction"])
                self.assertEqual(len(message.splitlines()), 3)
                self.assertIn(f"📊 第 {len(history) + 1} 局", message)
                self.assertIn("推薦方向：", message)
                self.assertIn("建議注數：", message)
                self.assertIn(tier, (0, 5, 10, 20))
                self.assertEqual(report["action"], "Skip")
                self.assertEqual(report["bankroll_fraction"], 0)
                self.assertIs(report["validated_edge"], False)

    def test_max_history_never_silently_truncated(self):
        self.call("開始分析")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["groups"]["C-group-1"]["history"] = ["B"] * 72
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("72 局上限", self.call("藍"))
        self.assertEqual(len(self.state()["history"]), 72)

    def test_gate_diagnostics_preserve_actual_prediction_and_report(self):
        from bbb_soft_shoe_runtime import analyze_history
        from group_text_research import _research

        # Real engine examples: B/P with a zero Kelly tier, and a true SKIP.
        for history in (list("BBB"), list("BBBBBBBB")):
            with self.subTest(hands=len(history)):
                report = analyze_history(history)
                original = copy.deepcopy(report)
                with patch.dict(os.environ, {"GROUP_RESEARCH_GATE_LOG": "0"}):
                    quiet = io.StringIO()
                    with redirect_stdout(quiet):
                        expected = _research(history, lambda _: report)
                self.assertEqual(quiet.getvalue(), "")
                output = io.StringIO()
                with patch.dict(os.environ, {
                    "GROUP_RESEARCH_GATE_LOG": "1",
                    "RENDER_GIT_COMMIT": "a" * 40,
                    "LINE_CHANNEL_ACCESS_TOKEN": "do-not-log-token",
                    "GROUP_ADMIN_UID": "do-not-log-uid",
                }), redirect_stdout(output):
                    actual = _research(history, lambda _: report)
                self.assertEqual(actual, expected)
                self.assertEqual(report, original)
                self.assertEqual(len(actual[2].splitlines()), 3)
                prefix, payload = output.getvalue().strip().split(" ", 1)
                self.assertEqual(prefix, "group_research_gate")
                record = json.loads(payload)
                self.assertEqual(record["repository_commit"], "a" * 40)
                self.assertEqual(record["research_direction"], report["research_view"]["direction"])
                self.assertEqual(record["reasons"], report["research_view"]["reasons"])
                self.assertEqual(record["kelly_tier"], 0)
                self.assertEqual(record["display_tier"], 5 if len(history) == 3 else 0)
                if len(history) == 3:
                    self.assertEqual(record["tier_basis"], "fixed_minimum")
                for key, value in record["gate_metrics"].items():
                    self.assertEqual(value, report["research_view"][key])
                for key, value in record["gate_thresholds"].items():
                    self.assertEqual(value, report["config"][key])
                self.assertNotIn("do-not-log", payload)
                self.assertNotIn('"history"', payload)
                self.assertEqual(report["action"], "Skip")
                self.assertEqual(report["bankroll_fraction"], 0)
                self.assertFalse(report["validated_edge"])


if __name__ == "__main__":
    unittest.main()
