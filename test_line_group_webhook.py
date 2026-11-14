"""Signed webhook split: group text route vs untouched private LINE route."""
import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from line_group_webhook import install_group_webhook


class LegacyStub:
    CHANNEL_SECRET = "local-signature-test-secret"

    def __init__(self):
        self.messages = []
        self.pushes = []
        self.visits = []
        self.scheduled = []
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

    def _push(self, target, messages, **kwargs):
        self.pushes.append((target, messages))
        return True

    def _schedule_background(self, coroutine):
        # Test without running a real OCR/background task.
        self.scheduled.append(coroutine)
        coroutine.close()

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

    def test_private_audit_query_uses_only_signed_sender_and_preserves_other_events(self):
        import prediction_audit as audit
        query = self.event({"type": "user", "userId": "U-regular"}, "query", "查詢成績")
        normal = self.event({"type": "user", "userId": "U-other"}, "normal", "84")
        with patch("line_group_webhook.audit.report_text", return_value="私人預測成績") as read:
            self.assertEqual(self.post([query, normal]).status_code, 200)
        self.assertEqual(read.call_args.args[1], audit.scope_key("user", "U-regular"))
        self.assertEqual(self.stub.messages[0][1][0]["text"], "私人預測成績")
        self.assertEqual(self.stub.visits, [[normal]])

    def test_private_audit_query_cannot_use_group_or_unsigned_identity(self):
        with patch("line_group_webhook.audit.report_text") as read:
            self.post([self.event({"type": "user", "userId": "U-regular"}, "query", "查詢成績")], valid=False)
            self.post([self.event({"type": "user"}, "missing-user", "查詢成績")])
            self.post([self.event({"type": "room", "userId": "U-regular"}, "room", "查詢成績")])
            with patch.dict(os.environ, {"GROUP_ADMIN_UID": "approved", "GROUP_ADMIN_UIDS": ""}):
                self.post([self.event({"type": "group", "userId": "outsider", "groupId": "C-good"}, "group", "查詢成績")])
        read.assert_not_called()
        self.assertEqual(self.stub.visits, [])
        self.assertEqual(self.stub.messages, [])

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
        self.assertIn("quickReply", self.stub.messages[0][1][0])
        handler.assert_called_once()
        self.assertEqual(self.stub.pushes, [])

    def test_failed_text_reply_falls_back_to_same_group(self):
        message = "📊 第 4 局\n推薦方向：🔵 閒\n建議注數：5 注（模擬）"
        event = self.event({"type": "group", "groupId": "C-good",
                            "userId": "U-admin"}, "expired-reply")
        with patch("line_group_webhook.handle_group_event", return_value=message), \
                patch.object(self.stub, "_reply", return_value=False):
            response = self.post([event])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.stub.pushes), 1)
        target, messages = self.stub.pushes[0]
        self.assertEqual(target, "C-good")
        self.assertEqual(messages[0]["text"], message)
        self.assertIn("quickReply", messages[0])
        self.assertEqual(self.stub.visits, [])

    def test_reply_failure_does_not_lose_or_duplicate_manual_hand(self):
        from bbb_soft_shoe_runtime import VERSION
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "GROUP_STATE_FILE": str(Path(directory) / "group.json"),
            "GROUP_TEXT_ENABLED": "1", "GROUP_ADMIN_UID": "U-admin",
            "GROUP_ADMIN_UIDS": "", "GROUP_ALLOWED_IDS": "C-good",
            "GROUP_RESEARCH_GATE_LOG": "0",
        }):
            source = {"type": "group", "groupId": "C-good", "userId": "U-admin"}
            self.post([self.event(source, "start", "開始分析")])
            report = {"version": VERSION, "action": "Skip", "validated_edge": False,
                      "bankroll_fraction": 0, "probabilities": {"B": .51, "P": .48, "T": .01},
                      "observations": 1, "candidate": "B", "ev_banker": .0045, "ev_player": -.03,
                      "research_view": {"direction": "B", "eligible": True,
                                        "abs_ev_gap": .0345, "ev_gap_se": .025, "ev_gap_snr": 1.38,
                                        "ess_ratio": .9, "ancestor_diversity": 1},
                      "road_aux": {"validated_betting_edge": False}}
            result = self.event(source, "hand-1", "閒3莊5")
            with patch("group_text_research.analyze_history", return_value=report) as model, \
                    patch.object(self.stub, "_reply", return_value=False):
                self.assertEqual(self.post([result]).status_code, 200)
                self.assertEqual(self.post([result]).status_code, 200)
            model.assert_called_once_with(["B"])
            self.assertEqual(len(self.stub.pushes), 1)
            self.assertEqual(self.stub.pushes[0][0], "C-good")
            self.assertIn("第 2 局", self.stub.pushes[0][1][0]["text"])
            self.assertNotIn("觀望", self.stub.pushes[0][1][0]["text"])
            self.assertIn("5 注", self.stub.pushes[0][1][0]["text"])
            state = json.loads((Path(directory) / "group.json").read_text())["groups"]["C-good"]
            self.assertEqual(state["history"], ["B"])

    def test_reply_exception_recovers_without_repeating_handler(self):
        event = self.event({"type": "group", "groupId": "C-good",
                            "userId": "U-admin"}, "reply-network-error")
        with patch("line_group_webhook.handle_group_event", return_value="測試回覆") as handler, \
                patch.object(self.stub, "_reply", side_effect=TimeoutError("request timeout")):
            response = self.post([event])
        self.assertEqual(response.status_code, 200)
        handler.assert_called_once()
        self.assertEqual(len(self.stub.pushes), 1)
        self.assertEqual(self.stub.pushes[0][0], "C-good")

    def test_failed_push_is_logged_without_repeating_model_or_exposing_ids(self):
        event = self.event({"type": "group", "groupId": "C-secret",
                            "userId": "U-secret"}, "private-token")
        with patch("line_group_webhook.handle_group_event", return_value="私密結果") as handler, \
                patch.object(self.stub, "_reply", return_value=False), \
                patch.object(self.stub, "_push", return_value=False) as push, \
                patch("builtins.print") as log:
            response = self.post([event])
        self.assertEqual(response.status_code, 200)
        handler.assert_called_once()
        push.assert_called_once()
        output = str(log.call_args_list)
        self.assertIn('"ok": false', output)
        for secret in ("C-secret", "U-secret", "private-token", "私密結果"):
            self.assertNotIn(secret, output)

    def test_signed_but_unauthorized_text_never_replies_or_pushes(self):
        with patch.dict(os.environ, {"GROUP_ADMIN_UID": "U-approved",
                                    "GROUP_ADMIN_UIDS": "", "GROUP_ALLOWED_IDS": "C-good"}):
            response = self.post([self.event({"type": "group", "groupId": "C-good",
                                              "userId": "U-outsider"}, "outsider")])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stub.messages, [])
        self.assertEqual(self.stub.pushes, [])

    def test_personal_non_uid_preserves_legacy_with_old_zero_setting(self):
        with patch.dict(os.environ, {"GROUP_ONLY_MODE": "0"}):
            res = self.post([self.event({
                "type": "user", "userId": "U-regular"
            }, "t1", text="hello")])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.stub.visits), 1)
        self.assertEqual(len(self.stub.visits[0]), 1)

    def test_old_group_only_setting_does_not_suppress_private_prediction(self):
        with patch.dict(os.environ, {"GROUP_ONLY_MODE": "1"}):
            res = self.post([self.event({
                "type": "user", "userId": "U-regular"
            }, "t2", text="開始分析")])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.stub.visits), 1)
        self.assertEqual(self.stub.visits[0][0]["message"]["text"], "開始分析")
        self.assertEqual(self.stub.messages, [])

    def test_all_private_event_types_reach_original_handler_by_default(self):
        source = {"type": "user", "userId": "U-personal"}
        text = self.event(source, "private-text", text="84")
        image = self.event(source, "private-image")
        image["message"] = {"type": "image", "id": "img-private"}
        postback = {"type": "postback", "source": source,
                    "replyToken": "private-button", "postback": {"data": "start"}}
        follow = {"type": "follow", "source": source, "replyToken": "private-follow"}
        events = [text, image, postback, follow]
        with patch.dict(os.environ):
            os.environ.pop("GROUP_ONLY_MODE", None)
            with patch("line_group_webhook.handle_group_event") as group, \
                    patch("line_group_webhook.reserve_image") as scan:
                response = self.post(events)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stub.visits, [events])
        group.assert_not_called()
        scan.assert_not_called()
        self.assertEqual(self.stub.messages, [])


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
        with patch.dict(os.environ, {"GROUP_ONLY_MODE": "0"}):
            with patch("line_group_webhook.handle_group_event",
                       return_value="研究觀望") as handler:
                res = self.post(events)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.stub.visits), 1)
        self.assertEqual(len(self.stub.visits[0]), 1)
        self.assertEqual(self.stub.visits[0][0]["replyToken"], "personal")
        self.assertEqual(len(self.stub.messages), 1)
        handler.assert_called_once()

    def test_old_group_only_mixed_batch_keeps_both_routes_separate(self):
        events = [
            self.event({
                "type": "group", "groupId": "C-good", "userId": "U-admin"
            }, "group"),
            self.event({"type": "user", "userId": "U-personal"},
                       "personal", text="測試"),
        ]
        with patch.dict(os.environ, {"GROUP_ONLY_MODE": "1"}):
            with patch("line_group_webhook.handle_group_event",
                       return_value="下一局研究：🔴 莊｜5 注") as handler:
                res = self.post(events)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.stub.visits, [[events[1]]])
        self.assertEqual(len(self.stub.messages), 1)
        self.assertEqual(self.stub.messages[0][0], "group")
        self.assertIn("下一局研究：🔴 莊｜5 注",
                      self.stub.messages[0][1][0]["text"])
        handler.assert_called_once()

    def test_mixed_uid_query_image_and_private_button_are_delivered_once(self):
        source = {"type": "user", "userId": "U-personal"}
        uid = self.event(source, "uid-token", text="查詢UID")
        image = self.event(source, "image-token")
        image["message"] = {"type": "image", "id": "private-image"}
        button = {"type": "postback", "source": source,
                  "replyToken": "button-token", "postback": {"data": "start"}}
        group = self.event({"type": "group", "userId": "U-admin", "groupId": "C-good"},
                           "group-token", text="84")
        with patch.dict(os.environ, {"GROUP_ONLY_MODE": "1"}), \
                patch("line_group_webhook.handle_group_event", return_value="群組結果") as handler:
            response = self.post([uid, image, group, button])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stub.visits, [[image, button]])
        self.assertEqual([token for token, _ in self.stub.messages], ["uid-token", "group-token"])
        self.assertEqual(self.stub.pushes, [])
        handler.assert_called_once_with(group)

    def test_unsupported_sources_cannot_enter_private_handler(self):
        events = [self.event({"type": "room", "roomId": "R-room", "userId": "U-user"}, "room"),
                  self.event({"userId": "U-user"}, "unknown")]
        with patch.dict(os.environ, {"GROUP_ONLY_MODE": "0"}), \
                patch("line_group_webhook.handle_group_event") as group:
            response = self.post(events)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stub.visits, [])
        self.assertEqual(self.stub.messages, [])
        group.assert_not_called()

    def test_bad_signature_never_reaches_private_or_group_handler(self):
        events = [self.event({"type": "user", "userId": "U-user"}, "private"),
                  self.event({"type": "group", "groupId": "C-good", "userId": "U-admin"}, "group")]
        with patch("line_group_webhook.handle_group_event") as group:
            response = self.post(events, valid=False)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.stub.visits, [])
        self.assertEqual(self.stub.messages, [])
        group.assert_not_called()

    def test_group_image_ack_is_group_reply_not_private_legacy_panel(self):
        event = self.event({
            "type": "group", "groupId": "C-ocr", "userId": "U-admin"
        }, "group-img")
        event["message"] = {"type": "image", "id": "img-123"}
        claim = {
            "group_id": "C-ocr", "message_id": "img-123",
            "event_id": "group-img", "run_id": "r1",
        }
        with patch("line_group_webhook.reserve_image", return_value=claim) as reserve:
            res = self.post([event])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.stub.visits, [])
        self.assertEqual(len(self.stub.messages), 1)
        self.assertEqual(self.stub.messages[0][0], "group-img")
        self.assertIn("牌路掃描中", self.stub.messages[0][1][0]["text"])
        self.assertIn("quickReply", self.stub.messages[0][1][0])
        self.assertEqual(len(self.stub.scheduled), 1)
        reserve.assert_called_once()

    def test_unknown_group_image_is_not_acknowledged_or_processed(self):
        event = self.event({
            "type": "group", "groupId": "C-unknown", "userId": "U-other"
        }, "unauthorized-image")
        event["message"] = {"type": "image", "id": "img-124"}
        with patch("line_group_webhook.reserve_image", return_value=None):
            res = self.post([event])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.stub.messages, [])
        self.assertEqual(self.stub.scheduled, [])
        self.assertEqual(self.stub.visits, [])

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
