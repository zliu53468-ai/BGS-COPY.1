"""LINE group screenshot OCR integration: authorized, accurate, isolated, no bankroll."""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from group_image_scan import reserve_image, finish_image, scan_and_reply_group
from group_text_research import VERSION, handle_group_event


class GroupOCRTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"
        self.env = {
            "GROUP_TEXT_ENABLED": "1", "GROUP_IMAGE_SCAN_ENABLED": "1",
            "GROUP_ADMIN_UID": "U-abc", "GROUP_ALLOWED_IDS": "C-good",
            "GROUP_STOP_LOSS_UNITS": "30",
        }
        self.n = 0
        self.predicted = []

    def event(self, text="開始分析", *, uid="U-abc", group="C-good",
              kind="text", event_id=None):
        self.n += 1
        n = event_id or f"e-{self.n}"
        return {
            "type": "message", "webhookEventId": n,
            "source": {"type": "group", "groupId": group, "userId": uid},
            "message": {"type": kind, "id": f"m-{self.n}", "text": text}
        }

    def start(self):
        return handle_group_event(self.event(), environ=self.env, state_path=self.path)

    def image(self, **kwargs):
        return self.event(text="", kind="image", **kwargs)

    def claim(self, event):
        return reserve_image(event, environ=self.env, state_path=self.path)

    def model(self, history):
        self.predicted.append(list(history))
        return {
            "version": VERSION, "action": "Skip",
            "validated_edge": False, "bankroll_fraction": 0,
            "observations": len(history), "candidate": "B",
            "ev_banker": .307, "ev_player": -.34,
            "probabilities": {"B": .66, "P": .32, "T": .02},
            "research_view": {
                "abs_ev_gap": .647, "ev_gap_se": .01, "ev_gap_snr": 64.7,
                "ess_ratio": .9, "ancestor_diversity": 1,
                "eligible": True, "direction": "B", "road_confirmed": True},
            "road_aux": {
                "estimated_only": True, "validated_betting_edge": False},
        }

    def screen(self, history):
        return {
            "recognition_quality_ok": True,
            "reconstructed_all": True,
            "raw_outcomes": history,
            "sequence": [x for x in history if x != "T"],
        }

    def done(self, claim, screen):
        return finish_image(
            claim, screen, environ=self.env,
            state_path=self.path, predictor=self.model)

    def state(self):
        return json.loads(self.path.read_text())["groups"]["C-good"]

    def test_ocr_forecast_is_scored_by_following_text_without_recounting_import(self):
        import prediction_audit as audit
        self.start()
        self.done(self.claim(self.image()), self.screen(["B", "P", "T"]))
        handle_group_event(self.event("38"), environ=self.env, state_path=self.path, predictor=self.model)
        path, scope = audit.database_path(self.path), audit.scope_key("group", "C-good")
        stats = audit.summarize(path, scope)
        self.assertEqual((stats["cohorts"][0]["forecasts"], stats["cohorts"][0]["wins"]), (2, 1))
        stale = self.claim(self.image())
        self.start()
        self.done(stale, self.screen(["B", "P", "T", "B", "P"]))
        self.assertEqual(audit.summarize(path, scope), stats)

    def test_authorization_and_active_session_before_download(self):
        self.assertIsNone(self.claim(self.image(uid="U-outsider")))
        self.assertIsNone(self.claim(self.image(group="C-outside")))
        self.assertIsNone(self.claim(self.image(uid="")))
        self.assertFalse(self.path.exists())
        self.assertIn("開始分析", self.claim(self.image()))
        self.start()
        accepted = self.claim(self.image(event_id="image-1"))
        self.assertEqual(accepted["group_id"], "C-good")
        self.assertEqual(accepted["event_id"], "image-1")
        self.assertEqual(accepted["run_id"], self.state()["run_id"])
        self.assertIsNone(self.claim(self.image(event_id="image-1")))
        self.env["GROUP_IMAGE_SCAN_ENABLED"] = "0"
        self.assertIsNone(self.claim(self.image()))
        self.assertEqual(self.state()["history"], [])

    def test_real_engine_observation_matches_text_and_clears_previous_image_tier(self):
        from bbb_soft_shoe_runtime import analyze_history
        from group_text_research import _research
        self.start()
        for raw, wanted_tier in (("BBB", 5), ("BBBBBBBB", 0)):
            history = list(raw)
            reply = finish_image(self.claim(self.image()), self.screen(history),
                                 environ=self.env, state_path=self.path,
                                 predictor=analyze_history)
            direction, tier, text = _research(history, analyze_history)
            self.assertEqual(reply, text)
            self.assertEqual(tier, wanted_tier)
            self.assertEqual(self.state()["last_research_direction"], direction)
            self.assertEqual(self.state()["pending_tier"], tier)
            self.assertEqual(self.state()["history"], history)

    def test_first_scan_and_incremental_scan_not_duplicated(self):
        self.start()
        first = self.image(event_id="img-a")
        claim = self.claim(first)
        history = ["B", "P", "T", "B", "B", "P", "T", "P"]
        result = self.done(claim, self.screen(history))
        self.assertIn("第 9 局", result)
        self.assertIn("推薦方向：🔴 莊", result)
        self.assertIn("建議注數：5 注（模擬）", result)
        self.assertEqual(len(result.splitlines()), 3)
        self.assertEqual(self.state()["history"], history)
        self.assertEqual(self.state()["net_units"], 0)
        self.assertNotIn("bankroll", self.state())
        duplicate = self.claim(self.image(event_id="img-b"))
        self.assertIn("未重複新增", self.done(duplicate, self.screen(history)))
        self.assertEqual(len(self.predicted), 1)
        more = self.claim(self.image(event_id="img-c"))
        extended = history + ["B", "P"]
        self.assertIn("第 11 局", self.done(more, self.screen(extended)))
        self.assertEqual(self.state()["history"], extended)

    def test_unsafe_or_incomplete_images_never_update_history(self):
        self.start()
        claim = self.claim(self.image())
        bad = self.screen(["B", "P"])
        bad["recognition_quality_ok"] = False
        self.assertIn("未更新分析", self.done(claim, bad))
        self.assertEqual(self.state()["history"], [])
        for input_seq in ([], ["B", "X"], ["B"] * 73):
            with self.subTest(input_seq=input_seq[:5]):
                claim = self.claim(self.image())
                result = self.done(claim, self.screen(input_seq))
                self.assertIn("未更新", result)
                self.assertEqual(self.state()["history"], [])

    def test_mismatched_history_and_old_group_run_fail_closed(self):
        self.start()
        first = self.claim(self.image())
        self.done(first, self.screen(["B", "P", "T"]))
        next_image = self.claim(self.image())
        self.assertIn("不一致", self.done(next_image, self.screen(["P", "P"])))
        self.assertEqual(self.state()["history"], ["B", "P", "T"])
        waiting_image = self.claim(self.image())
        self.start()  # Administrator changed run during slow OCR.
        self.assertIn("已忽略舊結果", self.done(
            waiting_image, self.screen(["B", "P", "T", "B"])))
        self.assertEqual(self.state()["history"], [])

    def test_end_analysis_clears_snapshot_and_discards_inflight_image(self):
        self.start()
        first = self.claim(self.image())
        self.done(first, self.screen(["B", "P", "T"]))
        self.assertEqual(self.state()["history"], ["B", "P", "T"])
        pending = self.claim(self.image())
        done = handle_group_event(
            self.event("結束分析"), environ=self.env, state_path=self.path)
        self.assertIn("已清空", done)
        self.assertFalse(self.state()["active"])
        self.assertEqual(self.state()["history"], [])
        self.assertEqual(self.state()["records"], [])
        self.assertIn("已忽略舊結果",
                      self.done(pending, self.screen(["B", "P", "T", "B"])))
        self.assertEqual(self.state()["history"], [])

    def test_wrong_wager_gate_is_rejected_and_not_persisted(self):
        self.start()
        claim = self.claim(self.image())
        def unsafe(hist):
            r = self.model(hist)
            r["action"] = "B"
            return r
        with self.assertRaisesRegex(RuntimeError, "安全鎖定"):
            finish_image(claim, self.screen(["B", "P"]), environ=self.env,
                         state_path=self.path, predictor=unsafe)
        self.assertEqual(self.state()["history"], [])

    def test_cancelled_worker_does_not_mutate_state(self):
        self.start()
        claim = self.claim(self.image())
        cancel = threading.Event()
        cancel.set()
        result = finish_image(
            claim, self.screen(["B", "P"]), environ=self.env,
            state_path=self.path, predictor=self.model, cancel_event=cancel)
        self.assertIn("逾時取消", result)
        self.assertEqual(self.state()["history"], [])

    def test_finish_while_paused_discards_image(self):
        self.start()
        claim = self.claim(self.image())
        handle_group_event(self.event("休息"), environ=self.env, state_path=self.path)
        self.assertIn("已忽略舊結果",
                      self.done(claim, self.screen(["B", "P"])))
        self.assertEqual(self.state()["history"], [])

    def test_background_push_targets_group_id_not_admin_uid(self):
        self.start()
        claim = self.claim(self.image())

        class FakeApp:
            def __init__(self, image_path):
                self.path = image_path
                self.sent = []

            def _download_line_image(self, message_id):
                return self.path

            def _prepare_analysis_image(self, path):
                return path

            def _text(self, text):
                return {"type": "text", "text": text}

            def _push(self, to, messages, **kwargs):
                self.sent.append((to, messages, kwargs))
                return True

        path = Path(self.directory.name) / "dummy.png"
        path.write_bytes(b"x")
        fake = FakeApp(path)
        with patch("screen_pipeline.analyze_game_screen",
                   return_value=self.screen(["B", "P"])):
            with patch("group_image_scan.finish_image",
                       return_value="這是群組 OCR 預測結果") as finish:
                asyncio.run(scan_and_reply_group(fake, claim))
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0][0], "C-good")
        self.assertNotEqual(fake.sent[0][0], "U-abc")
        self.assertIn("群組 OCR", fake.sent[0][1][0]["text"])
        self.assertIn("quickReply", fake.sent[0][1][0])
        self.assertLessEqual(len(fake.sent[0][1][0]["quickReply"]["items"]), 13)
        finish.assert_called_once()

    def test_real_manual_and_screenshot_histories_have_identical_next_forecast(self):
        self.env["GROUP_ALLOWED_IDS"] = "C-good,C-manual"
        for history in (list("BBB"), list("BBBBBBBB"),
                        list("BPBPBBBBBBBBPPPPPTTPPPBP")):
            with self.subTest(hands=len(history)):
                self.start()
                claim = self.claim(self.image())
                image_result = finish_image(claim, self.screen(history),
                                            environ=self.env, state_path=self.path)
                handle_group_event(self.event("開始分析", group="C-manual"),
                                   environ=self.env, state_path=self.path)
                for outcome in history:
                    manual_result = handle_group_event(
                        self.event({"B": "19", "P": "91", "T": "55"}[outcome], group="C-manual"),
                        environ=self.env, state_path=self.path)
                self.assertEqual(manual_result, image_result)
                self.assertIn("建議注數：", manual_result)
                states = json.loads(self.path.read_text())["groups"]
                self.assertEqual(states["C-good"]["history"], states["C-manual"]["history"])
                self.assertEqual(states["C-good"]["pending_tier"], states["C-manual"]["pending_tier"])
                self.assertEqual(states["C-good"]["last_simulation_tier"],
                                 states["C-manual"]["last_simulation_tier"])

    def test_manual_result_continues_screenshot_history_without_reset(self):
        from bbb_soft_shoe_runtime import analyze_history
        from group_text_research import _research
        history = list("BPPBTBBPPTBP")
        self.start()
        claim = self.claim(self.image())
        finish_image(claim, self.screen(history), environ=self.env, state_path=self.path)
        result = handle_group_event(self.event("莊"), environ=self.env, state_path=self.path)
        extended = history + ["B"]
        expected_direction, expected_tier, expected_text = _research(extended, analyze_history)
        self.assertEqual(result, expected_text)
        self.assertIn("第 14 局", result)
        self.assertEqual(self.state()["history"], extended)
        self.assertEqual(self.state()["last_research_direction"], expected_direction)
        self.assertEqual(self.state()["pending_tier"], expected_tier)


if __name__ == "__main__":
    unittest.main()
