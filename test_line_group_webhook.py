"""Signed webhook split: group text route vs untouched private LINE route."""
import base64
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from line_group_webhook import install_group_webhook


class LegacyStub:
    CHANNEL_SECRET = "local-signature-test-secret"

    def __init__(self):
        self.messages = []
        self.visits = []
        import traceback
        self.traceback = traceback

    def _verify_signature(self, body, sig):
        want = base64.b64encode(
            hmac.new(self.CHANNEL_SECRET.encode(), body,
                     hashlib.sha256).digest()
        ).decode()
        return bool(sig) and hmac.compare_digest(sig, want)

    @staticmethod
    def _text(text):
        return {"type": "text", "text": text}

    def _reply(self, token, messages):
        self.messages.append((token, messages))
        return True

    async def webhook(self, request):
        body = await request.body()
        if not self._verify_signature(
            body, request.headers.get("X-Line-Signature")
        ):
            return JSONResponse({"ok": False}, status_code=401)
        data = json.loads(body)
        self.visits.append(data.get("events"))
        return JSONResponse({"ok": True, "legacy": True})


class WebhookRouteTests(unittest.TestCase):
    def setUp(self):
        self.stub = LegacyStub()
        self.app = FastAPI()

        @self.app.post("/webhook")
        async def legacy(request):
            return await self.stub.webhook(request)

        install_group_webhook(self.app, self.stub)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def post(self, events, valid=True):
        body = json.dumps({"events": events}, ensure_ascii=False).encode()
        secret = self.stub.CHANNEL_SECRET if valid else "wrong"
        signature = base64.b64encode(
            hmac.new(secret.encode(), body, hashlib.sha256).digest()
        ).decode()
        return self.client.post(
            "/webhook", content=body,
            headers={"X-Line-Signature": signature,
                     "Content-Type": "application/json"},
        )

    def event(self, source, token, text="紅"):
        return {
            "type": "message", "replyToken": token,
            "source": source,
            "webhookEventId": token,
            "message": {"type": "text", "text": text, "id": token},
        }

    def test_only_one_route_is_active(self):
        routes = [
            x for x in self.app.router.routes
            if getattr(x, "path", "") == "/webhook"
            and "POST" in (getattr(x, "methods", ()) or ())
        ]
        self.assertEqual(len(routes), 1)

    def test_verify_empty_events_preserves_original(self):
        response = self.post([])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stub.visits, [[]])

    def test_bad_signature_rejected_before_any_mutation(self):
        with patch("line_group_webhook.handle_group_event") as handler:
            resp = self.post([self.event({
                "type": "group", "groupId": "C-good", "userId": "U-admin"
            }, "event-1")], valid=False)
        self.assertEqual(resp.status_code, 401)
        handler.assert_not_called()
        self.assertFalse(self.stub.messages)

    def test_group_message_only_uses_group_handler(self):
        with patch("line_group_webhook.handle_group_event", return_value="研究觀望") as handler:
            resp = self.post([self.event({
                "type": "group", "groupId": "C-good", "userId": "U-admin"
            }, "event-1")])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.stub.visits, [])
        self.assertEqual(self.stub.messages[0][1][0]["text"], "研究觀望")
        handler.assert_called_once()

    def test_personal_non_uid_preserves_legacy(self):
        res = self.post([self.event({
            "type": "user", "userId": "U-regular"
        }, "t1", text="hello")])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.stub.visits), 1)
        self.assertEqual(len(self.stub.visits[0]), 1)

    def test_private_uid_request_never_reaches_legacy(self):
        res = self.post([self.event({
            "type": "user", "userId": "U-self"
        }, "self", text="查詢UID")])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.stub.visits, [])
        self.assertIn("U-self", self.stub.messages[0][1][0]["text"])

    def test_mixed_webhook_preserves_user_events_once(self):
        events = [
            self.event({
                "type": "group", "groupId": "C-good", "userId": "U-admin"
            }, "group"),
            self.event({"type": "user", "userId": "U-personal"},
                       "personal", text="測試"),
        ]
        with patch("line_group_webhook.handle_group_event",
                   return_value="研究觀望") as handler:
            res = self.post(events)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.stub.visits), 1)
        self.assertEqual(len(self.stub.visits[0]), 1)
        self.assertEqual(self.stub.visits[0][0]["replyToken"], "personal")
        self.assertEqual(len(self.stub.messages), 1)
        handler.assert_called_once()

    def test_missing_user_id_group_cannot_respond(self):
        with patch("line_group_webhook.handle_group_event") as handler:
            resp = self.post([self.event({
                "type": "group", "groupId": "C-good"
            }, "missing")])
        self.assertEqual(resp.status_code, 200)
        handler.assert_not_called()
        self.assertFalse(self.stub.messages)


if __name__ == "__main__":
    unittest.main()
