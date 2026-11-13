"""LINE group mobile quick-reply controls (not a persistent rich menu).

LINE officially supports Quick Reply in groups, but the native
/v2/bot/chat/loading/start animation is ONE-TO-ONE ONLY. Group image
scanning uses a short progress reply; never make an invalid loading API call.
"""
from __future__ import annotations

from typing import Any


def quick_reply_items() -> list[dict[str, Any]]:
    """Actions are LINE Messaging API quick reply JSON schema, max 13."""
    def message(label: str, text: str) -> dict[str, Any]:
        return {
            "type": "action",
            "action": {"type": "message", "label": label, "text": text},
        }

    return [
        message("▶ 開始分析", "開始分析"),
        message("✍ 點數格式", "點數格式"),
        {
            "type": "action",
            "action": {"type": "cameraRoll", "label": "📷 上傳牌路"},
        },
        message("📊 查詢狀態", "查詢狀態"),
        message("🔴 莊", "紅"),
        message("🔵 閒", "藍"),
        message("🟢 和", "和"),
        message("⏹ 結束分析", "結束分析"),
    ]


def group_message(text: str) -> dict[str, Any]:
    """Attach group-friendly bottom buttons to every group text response."""
    return {
        "type": "text",
        "text": str(text)[:5000],
        "quickReply": {"items": quick_reply_items()},
    }
