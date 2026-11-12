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
            "probabilities": {"B": .66, "P": .32, "T": .02},
            "research_view": {
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

    def test_first_scan_and_incremental_scan_not_duplicated(self):
        self.start()
        first = self.image(event_id="img-a")
        claim = self.claim(first)
        history = ["B", "P", "T", "B", "B", "P", "T", "P"]
        result = self.done(claim, self.screen(history))
        self.assertIn("📷 群組圖片掃描完成", result)
        self.assertIn("已辨識：8 局", result)
        self.assertIn("本次新增：8 局", result)
        self.assertIn("下一局研究：🔴 莊｜5 注", result)
        self.assertIn("正式決策：Skip／0 注", result)
        self.assertEqual(self.state()["history"], history)
        self.assertEqual(self.state()["net_units"], 0)
        self.assertNotIn("bankroll", self.state())
        duplicate = self.claim(self.image(event_id="img-b"))
        self.assertIn("未重複新增", self.done(duplicate, self.screen(history)))
        self.assertEqual(len(self.predicted), 1)
        more = self.claim(self.image(event_id="img-c"))
        extended = history + ["B", "P"]
        self.assertIn("本次新增：2 局", self.done(more, self.screen(extended)))
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
        finish.assert_called_once()


if __name__ == "__main__":
    unittest.main()
