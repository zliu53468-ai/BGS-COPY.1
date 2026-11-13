"""Private points: signed routing, membership, UID state and exact group parity."""
import ast
import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from typing import Any, Dict
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import group_text_research as group
import line_private_points as private
import prediction_audit as audit
import store
from line_group_webhook import install_group_webhook


class PrivatePointsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "sessions.json"
        self.patch = patch.object(store, "SESSION_DATA_FILE", self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.messages, self.pushes, self.forwarded = [], [], []
        self.count = 0
        self.legacy = SimpleNamespace(
            store=store, CHANNEL_SECRET="test-secret", ALL_CODES={"test-code"},
            _normalize_access_code=lambda value: value.strip(),
            VENUE_BY_CODE={"DG": {}, "MT": {}},
            _text=lambda text: {"type": "text", "text": text},
            venue_panel=lambda uid: {"type": "text", "text": "請選館"},
            bankroll_panel=lambda uid, session: {"type": "text", "text": "請設定本金"},
            upload_request_panel=lambda uid, session: {"type": "text", "text": "請上傳圖片"},
            _request_bankroll=store.request_bankroll,
            _PREDICTION_SLOTS=threading.Semaphore(1),
            AccessExpiredError=type("AccessExpiredError", (Exception,), {}),
        )
        locks = {}
        self.legacy._user_image_lock = lambda uid: locks.setdefault(uid, threading.Lock())
        self.legacy._reply = lambda token, messages: self.messages.append((token, messages)) or True
        self.legacy._push = lambda target, messages, **kw: self.pushes.append((target, messages)) or True
        self.legacy._verify_signature = lambda body, sig: sig == self.sign(body)
        # Execute the original permission and screenshot-entry functions, not
        # rewritten approximations, without loading the OCR service at import.
        tree = ast.parse((Path(__file__).parent / "app.py").read_text())
        names = {"_ensure_access", "_start_screen_flow", "_start_new_uid_analysis", "_end_uid_analysis"}
        env = {**vars(self.legacy), "Dict": Dict, "Any": Any}
        exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)
                                     and n.name in names], type_ignores=[]), "app.py", "exec"), env)
        for name in names:
            setattr(self.legacy, name, env[name])

        async def original(request):
            body = await request.body()
            self.assertTrue(self.legacy._verify_signature(body, request.headers.get("X-Line-Signature")))
            events = json.loads(body)["events"]
            self.forwarded.extend(events)
            for event in events:
                if (event.get("message") or {}).get("text") in self.legacy.ALL_CODES:
                    store.upsert_session(event["source"]["userId"], {"permanent_access": True})
            return JSONResponse({"ok": True})
        self.legacy.webhook = original
        app = FastAPI()
        app.post("/webhook")(original)
        observer = store.update_screen_analysis
        self.addCleanup(setattr, store, "update_screen_analysis", observer)
        install_group_webhook(app, self.legacy)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def sign(self, body):
        return base64.b64encode(hmac.new(b"test-secret", body, hashlib.sha256).digest()).decode()

    def event(self, text="", uid="U-one", event_id=None, action=None):
        self.count += 1
        eid = event_id or f"evt-{self.count}"
        event = {"type": "message", "source": {"type": "user", "userId": uid},
                 "webhookEventId": eid, "replyToken": eid,
                 "message": {"type": "text", "id": eid, "text": text}}
        if action:
            event.update(type="postback", postback={"data": action})
            event.pop("message")
        return event

    def post(self, events, valid=True):
        body = json.dumps({"events": events}).encode()
        return self.client.post("/webhook", content=body, headers={
            "X-Line-Signature": self.sign(body) if valid else "wrong"})

    def call(self, text="", **kwargs):
        return private.handle_event(self.legacy, self.event(text, **kwargs))

    def start(self, uid="U-one"):
        store.select_venue(uid, "DG")
        return self.call("開始分析", uid=uid)

    def session(self, uid="U-one"):
        return store.get_session(uid)

    def fake_model(self, history):
        return {"version": group.VERSION, "action": "Skip", "validated_edge": False,
                "bankroll_fraction": 0, "observations": len(history), "candidate": "B",
                "probabilities": {"B": .51, "P": .48, "T": .01},
                "ev_banker": .0045, "ev_player": -.03,
                "research_view": {"direction": "SKIP", "eligible": False},
                "road_aux": {"validated_betting_edge": False}}

    def test_start_requires_venue_then_selection_accepts_points_without_bankroll(self):
        self.assertIn("請選館", self.call("開始分析")[0]["text"])
        self.post([self.event(action="action=venue&venue=DG&room=1")])
        state = self.session()
        self.assertTrue(state["point_score_mode"])
        self.assertFalse(state["awaiting_bankroll"])
        self.assertFalse(state["awaiting_screenshot"])
        self.assertEqual(state["bankroll"], 0)
        with patch.object(group, "analyze_history", side_effect=self.fake_model):
            self.post([self.event("84")])
        self.assertEqual(self.session()["raw_outcomes"], ["P"])
        text = self.messages[-1][1][0]["text"]
        self.assertEqual(len(text.splitlines()), 3)
        self.assertIn("第 2 局", text)
        self.assertIn("5 注", text)
        self.assertFalse(self.forwarded)

    def test_real_engine_and_three_line_result_match_group_for_entire_shoe_prefix(self):
        self.start()
        for pair, expected in (("84", "P"), ("莊7閒2", "PB"), ("９９", "PBT")):
            actual = self.call(pair)[0]["text"]
            direction, tier, wanted = group._research(list(expected), group.analyze_history)
            self.assertEqual(actual, wanted)
            pred = self.session()["screen_last_prediction"]
            self.assertEqual(pred["bbb_soft_shoe"]["report"], group.analyze_history(list(expected)))
            self.assertEqual((pred["direction"], pred["simulation_tier"]), (direction, tier))
            self.assertEqual((pred["formal_action"], pred["bet_amount"]), ("Skip", 0))

    def test_two_users_and_group_namespaces_stay_separate(self):
        self.start("U-one")
        self.start("U-two")
        with patch.object(group, "analyze_history", side_effect=self.fake_model):
            self.call("84", uid="U-one")
            self.call("38", uid="U-two")
            self.call("38", uid="U-one")
        self.assertEqual(self.session("U-one")["raw_outcomes"], ["P", "B"])
        self.assertEqual(self.session("U-two")["raw_outcomes"], ["B"])
        path = audit.database_path(self.path)
        self.assertEqual(audit.summarize(path, audit.scope_key("user", "U-one"))["cohorts"][0]["wins"], 1)
        self.assertEqual(audit.summarize(path, audit.scope_key("user", "U-two"))["cohorts"][0]["resolved"], 0)
        self.assertEqual(audit.summarize(path, audit.scope_key("group", "U-one"))["cohorts"], [])
        self.assertFalse(self.path.with_name("group_research_state.json").exists())

    def test_batch_calculates_once_and_does_not_fabricate_past_forecasts(self):
        self.start()
        with patch.object(group, "analyze_history", side_effect=self.fake_model) as model:
            self.call("84\n38\n99")
            model.assert_called_once_with(["P", "B", "T"])
        stats = audit.summarize(audit.database_path(self.path), audit.scope_key("user", "U-one"))
        self.assertEqual((stats["observations"], stats["cohorts"][0]["forecasts"], stats["cohorts"][0]["resolved"]), (3, 1, 0))

    def test_nonconsecutive_redelivery_and_old_start_never_duplicate_or_reset(self):
        store.select_venue("U-one", "DG")
        self.call("開始分析", event_id="start")
        with patch.object(group, "analyze_history", side_effect=self.fake_model) as model:
            self.call("84", event_id="a")
            self.call("38", event_id="b")
            self.assertEqual(self.call("84", event_id="a"), [])
            self.assertEqual(self.call("開始分析", event_id="start"), [])
            self.assertEqual(model.call_count, 2)
        self.assertEqual(self.session()["raw_outcomes"], ["P", "B"])

    def test_invalid_batch_overflow_and_missing_event_do_not_mutate_history(self):
        self.start()
        with patch.object(group, "analyze_history", side_effect=self.fake_model) as model:
            for text in ("10,9", "84\ninvalid", "84\n" * 21, "10000"):
                self.assertIn("未記錄", self.call(text)[0]["text"])
            model.assert_not_called()
            store.upsert_session("U-one", {"raw_outcomes": ["B"] * 72})
            self.assertIn("72 局", self.call("84")[0]["text"])
            model.assert_not_called()
        event = self.event("84")
        event.pop("webhookEventId")
        event["message"].pop("id")
        self.assertIn("事件 ID", private.handle_event(self.legacy, event)[0]["text"])
        self.assertEqual(len(self.session()["raw_outcomes"]), 72)

    def test_restart_or_end_during_computation_rejects_stale_result(self):
        for restart in (True, False):
            self.start()
            def interrupt(history):
                self.call("開始分析" if restart else "結束分析")
                return self.fake_model(history)
            with patch.object(group, "analyze_history", side_effect=interrupt):
                self.assertIn("舊點數未寫入", self.call("84")[0]["text"])
            self.assertEqual(self.session()["raw_outcomes"], [])

    def test_membership_and_bankroll_are_preserved_and_expiry_blocks_model(self):
        store.select_venue("U-one", "DG")
        store.upsert_session("U-one", {"permanent_access": True, "bankroll": 30000, "awaiting_bankroll": True})
        self.call("開始分析")
        with patch.object(group, "analyze_history", side_effect=self.fake_model):
            self.call("84")
        self.assertEqual(self.session()["bankroll"], 30000)
        self.assertTrue(self.session()["permanent_access"])
        self.call("結束分析")
        self.assertEqual(self.session()["point_score_audit"], [])
        self.assertEqual(self.session()["bankroll"], 30000)
        self.start()
        store.upsert_session("U-one", {"permanent_access": False, "trial_started_at": 1, "access_until": 2})
        with patch.object(group, "analyze_history") as model:
            self.assertIn("試用已到期", self.call("84")[0]["text"])
            model.assert_not_called()

    def test_original_image_mode_can_be_selected_and_keeps_its_bankroll_requirement(self):
        self.start()
        self.assertIn("請設定本金", self.call("圖片模式")[0]["text"])
        self.assertFalse(self.session()["point_score_mode"])
        self.assertTrue(self.session()["awaiting_bankroll"])
        self.assertIsNone(self.call("10000"))
        self.assertIsNone(self.call(action="action=reset"))
        self.assertIsNone(self.call("結束分析"))

    def test_bad_signature_and_group_event_never_enter_private_mode(self):
        with patch.object(private, "handle_event", wraps=private.handle_event) as handler:
            self.assertEqual(self.post([self.event("開始分析")], valid=False).status_code, 401)
            event = self.event("開始分析")
            event["source"] = {"type": "group", "groupId": "C-group", "userId": "U-one"}
            with patch.dict(os.environ, {"GROUP_ADMIN_UID": "", "GROUP_ADMIN_UIDS": ""}):
                self.post([event])
            handler.assert_not_called()

    def test_activation_precedes_start_in_same_batch_even_for_score_like_code(self):
        self.legacy.ALL_CODES = {"84"}
        store.select_venue("U-one", "DG")
        store.upsert_session("U-one", {"permanent_access": False, "trial_started_at": 1, "access_until": 2})
        self.post([self.event("84"), self.event("開始分析")])
        self.assertTrue(self.session()["permanent_access"])
        self.assertTrue(self.session()["point_score_mode"])
        self.assertEqual(self.session()["raw_outcomes"], [])
        self.assertEqual(len(self.forwarded), 1)

    def test_private_reply_failure_pushes_same_uid_once_without_recalculation(self):
        self.start()
        event = self.event("84")
        with patch.object(group, "analyze_history", side_effect=self.fake_model) as model, \
                patch.object(self.legacy, "_reply", side_effect=TimeoutError("reply expired")):
            self.assertEqual(self.post([event]).status_code, 200)
            self.assertEqual(self.post([event]).status_code, 200)
            model.assert_called_once()
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.pushes[0][0], "U-one")
        self.assertIn("第 2 局", self.pushes[0][1][0]["text"])


if __name__ == "__main__":
    unittest.main()
