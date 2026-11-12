"""Fail-closed, isolated LINE group research text mode regression tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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
        self.assertIn("下一局研究：🔴 莊｜5 注", message)
        self.assertIn("累計注數：+0 注", message)
        self.assertIn("正式決策：Skip／0 注", message)

    def test_start_outcomes_and_tie_are_group_only_and_skip(self):
        self.assertIn("已開始", self.call("開始分析"))
        msg = self.call("紅")
        self.assertIn("第 1 局", msg)
        self.assertIn("🔴 莊", msg)
        self.assertIn("Kelly 模擬分級：5 注", msg)
        self.assertIn("正式決策：Skip／0 注", msg)
        self.assertIn("⚠️ 未驗證獲利優勢", msg)
        self.assertIn("🔵 閒", self.call("藍"))
        self.assertIn("🟢 和", self.call("和"))
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

    def test_no_soft_eligibility_means_observe(self):
        def no_edge(history):
            report = self.predictor(history)
            report["research_view"]["eligible"] = False
            return report
        self.call("開始分析")
        result = handle_group_event(
            self.event("紅"), environ=self.env,
            predictor=no_edge, state_path=self.path)
        self.assertIn("⚪ 觀望", result)
        self.assertIn("模擬分級：0 注", result)
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
        self.assertIn("已結束", self.call("結束分析"))
        self.assertFalse(self.state()["active"])
        self.assertIn("開始分析", self.call("紅"))
        self.assertEqual(self.state()["history"], ["B"])

    def test_max_history_never_silently_truncated(self):
        self.call("開始分析")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["groups"]["C-group-1"]["history"] = ["B"] * 72
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("72 局上限", self.call("藍"))
        self.assertEqual(len(self.state()["history"]), 72)


if __name__ == "__main__":
    unittest.main()
