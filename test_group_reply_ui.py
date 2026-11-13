"""LINE mobile group Quick Reply schema contract, independent of OCR/model."""
from __future__ import annotations

import unittest

from group_reply_ui import group_message, quick_reply_items


class QuickReplyTest(unittest.TestCase):
    def test_supported_group_actions_and_count(self):
        buttons = quick_reply_items()
        self.assertLessEqual(len(buttons), 13)
        self.assertEqual(len(buttons), 9)
        labels = [item["action"]["label"] for item in buttons]
        self.assertEqual(len(set(labels)), len(labels))
        for item in buttons:
            self.assertEqual(item["type"], "action")
            self.assertLessEqual(len(item["action"]["label"]), 20)
            self.assertIn(item["action"]["type"], ("message", "cameraRoll"))
        image = next(item["action"] for item in buttons if item["action"]["type"] == "cameraRoll")
        self.assertEqual(image, {"type": "cameraRoll", "label": "📷 上傳牌路"})
        commands = [item["action"].get("text") for item in buttons]
        for command in ("開始分析", "點數格式", "查詢狀態", "查詢成績", "紅", "藍", "和", "結束分析"):
            self.assertIn(command, commands)

    def test_message_contract_and_standalone_button_definitions(self):
        message = group_message("📊 第 20 局（下一局）\n研究方向：🔴 莊\n模擬配注：5 注")
        self.assertEqual(message["type"], "text")
        self.assertIn("第 20 局", message["text"])
        self.assertIn("quickReply", message)
        # Fresh copies: neither message modifies another or global button state.
        message["quickReply"]["items"][0]["action"]["text"] = "tampered"
        self.assertEqual(
            group_message("testing")["quickReply"]["items"][0]["action"]["text"],
            "開始分析",
        )


if __name__ == "__main__":
    unittest.main()
