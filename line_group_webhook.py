"""Install isolated LINE group text router on the existing BGS FastAPI app.

Protected app.py remains byte-identical. This wrapper verifies the ORIGINAL
LINE signature and sends only group commands to the new group research module.
Individual LINE OCR, sessions and LIFF continue using legacy_app.webhook.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

from fastapi import Request
from fastapi.responses import JSONResponse

from group_text_research import handle_group_event
from group_image_scan import reserve_image, scan_and_reply_group
from group_reply_ui import group_message


async def _deliver_group_text(legacy_app, event, text: str) -> bool:
    """Reply once; recover a failed reply by pushing to the original group.

    The handler has already validated the sender/group and persisted the event.
    Never run it again to retry delivery, or a hand could be counted twice.
    """
    source = event.get("source") or {}
    group_id = str(source.get("groupId") or "")
    if source.get("type") != "group" or not group_id:
        return False
    messages = [group_message(text)]
    token = str(event.get("replyToken") or "")
    try:
        if token and await asyncio.to_thread(legacy_app._reply, token, messages):
            return True
    except Exception as exc:
        # A timeout can leave reply delivery uncertain. Retry the message only,
        # never the model/state mutation; do not expose tokens or message text.
        print("group_text_reply_error", type(exc).__name__, flush=True)
    try:
        sent = await asyncio.to_thread(
            legacy_app._push, group_id, messages, max_retries=0)
    except Exception as exc:
        print("group_text_push_error", type(exc).__name__, flush=True)
        sent = False
    print("group_text_delivery", json.dumps({
        "method": "push_fallback", "ok": bool(sent),
    }), flush=True)
    return bool(sent)


def install_group_webhook(app, legacy_app) -> None:
    original_webhook = legacy_app.webhook
    original_routes = [
        route for route in app.router.routes
        if not (
            getattr(route, "path", "") == "/webhook"
            and "POST" in (getattr(route, "methods", ()) or ())
        )
    ]
    if len(original_routes) == len(app.router.routes):
        raise RuntimeError("BGS existing /webhook not found; refuse to replace")
    app.router.routes[:] = original_routes

    @app.post("/webhook")
    async def group_text_or_legacy_webhook(request: Request) -> JSONResponse:
        body = await request.body()
        if not legacy_app._verify_signature(
            body, request.headers.get("X-Line-Signature")
        ):
            return JSONResponse({"ok": False}, status_code=401)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeError, ValueError):
            return JSONResponse({"ok": False}, status_code=400)
        events = payload.get("events", [])
        if not isinstance(events, list):
            return JSONResponse({"ok": False}, status_code=400)
        if not events:
            return await original_webhook(request)
        # Both chat types are supported. The retired GROUP_ONLY_MODE setting
        # must not silently discard private text, images, follows or postbacks.
        # Only explicit user sources may enter the unchanged personal handler.

        def is_uid_request(event) -> bool:
            source = event.get("source") or {}
            message = event.get("message") or {}
            return (
                source.get("type") == "user"
                and event.get("type") == "message"
                and message.get("type") == "text"
                and str(message.get("text") or "").strip() in ("查詢UID", "我的UID")
            )

        if all(
            (event.get("source") or {}).get("type") == "user"
            and not is_uid_request(event) for event in events
        ):
            return await original_webhook(request)

        remaining = []
        for event in events:
            source = event.get("source") or {}
            token = str(event.get("replyToken") or "")
            if source.get("type") == "group":
                if not source.get("userId"):
                    continue
                if (event.get("message") or {}).get("type") == "image":
                    try:
                        image_claim = await asyncio.to_thread(reserve_image, event)
                        if isinstance(image_claim, dict):
                            # Acknowledge in the SAME group, then perform OCR
                            # asynchronously and push ONLY to groupId.
                            # LINE official loading animations are unsupported
                            # in groups, so use a short progress reply instead.
                            legacy_app._reply(token, [group_message(
                                "⏳ 牌路掃描中，完成後直接回覆本群組。"
                            )])
                            legacy_app._schedule_background(
                                scan_and_reply_group(legacy_app, image_claim)
                            )
                        elif isinstance(image_claim, str):
                            legacy_app._reply(token, [group_message(image_claim)])
                    except Exception:
                        legacy_app.traceback.print_exc()
                        legacy_app._reply(token, [group_message(
                            "⚠️ 無法開始掃描，牌路未更新。"
                        )])
                    continue
                try:
                    reply = await asyncio.to_thread(handle_group_event, event)
                except Exception:
                    legacy_app.traceback.print_exc()
                    reply = "⚠️ 群組研究指令處理失敗，本次沒有更新紀錄。"
                if reply:
                    await _deliver_group_text(legacy_app, event, reply)
            elif is_uid_request(event):
                uid = str(source.get("userId") or "").strip()
                if uid:
                    legacy_app._reply(token, [legacy_app._text(
                        f"你的 LINE UID：{uid}\n請在 Render 的 GROUP_ADMIN_UIDS 設定此 UID。"
                    )])
            elif source.get("type") == "user":
                remaining.append(event)

        if remaining:
            # LINE may batch multiple events. Re-sign only the remaining
            # events AFTER verifying the full original LINE signature.
            # This prevents authorized group messages accidentally entering
            # the unmodified personal UID/OCR handler.
            subset = json.dumps(
                {**payload, "events": remaining},
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
            secret = legacy_app.CHANNEL_SECRET
            signature = base64.b64encode(
                hmac.new(secret.encode(), subset, hashlib.sha256).digest()
            ).decode("ascii")

            class VerifiedSubsetRequest:
                headers = {"X-Line-Signature": signature}

                async def body(self) -> bytes:
                    return subset

            return await original_webhook(VerifiedSubsetRequest())
        return JSONResponse({"ok": True})
