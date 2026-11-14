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
from unittest.mock import patch, Mock

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import group_text_research as group
import line_private_points as private
import prediction_audit as audit
import store
from line_group_webhook import install_group_webhook


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def panel_text(value):
    return "\n".join(n["text"] for n in walk(value) if n.get("type") == "text")


def button_data(value, label):
    return next(n["data"] for n in walk(value) if n.get("type") == "postback" and n.get("label") == label)


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
            guide_panel=lambda: {"type": "text", "text": "舊版指南"},
            _start_loading_animation=Mock(return_value=True),
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

    def finish_selection(self, uid="U-one"):
        flow = self.session(uid)["private_points_flow"]
        game = self.call(action=f"action=points_venue&venue=DG&flow={flow}", uid=uid)
        return self.call(action=button_data(game[0], "百家樂"), uid=uid)

    def start(self, uid="U-one"):
        self.call("開始分析", uid=uid)
        return self.finish_selection(uid)

    def session(self, uid="U-one"):
        return store.get_session(uid)

    def fake_model(self, history):
        return {"version": group.VERSION, "action": "Skip", "validated_edge": False,
                "bankroll_fraction": 0, "observations": len(history), "candidate": "B",
                "probabilities": {"B": .51, "P": .48, "T": .01},
                "ev_banker": .0045, "ev_player": -.03,
                "research_view": {"direction": "SKIP", "eligible": False,
                                  "abs_ev_gap": .0345, "ev_gap_se": .025, "ev_gap_snr": 1.38,
                                  "ess_ratio": .9, "ancestor_diversity": 1},
                "road_aux": {"validated_betting_edge": False}}

    def test_start_venue_game_input_and_result_follow_actual_buttons(self):
        menu = self.call("開始分析")[0]
        self.assertEqual(menu["type"], "flex")
        self.assertIn("請選擇遊戲平台", panel_text(menu))
        self.post([self.event(action=button_data(menu, "DG真人"))])
        game = self.messages[-1][1][0]
        self.assertIn("請選擇遊戲廳", panel_text(game))
        self.assertFalse(self.session()["point_score_mode"])
        self.post([self.event(action=button_data(game, "百家樂"))])
        prompt = self.messages[-1][1][0]
        self.assertIn("請輸入閒莊點數", panel_text(prompt))
        self.assertIn("例如：84", panel_text(prompt))
        state = self.session()
        self.assertTrue(state["point_score_mode"])
        self.assertFalse(state["awaiting_bankroll"])
        self.assertFalse(state["awaiting_screenshot"])
        self.assertEqual(state["bankroll"], 0)
        with patch.object(group, "analyze_history", side_effect=self.fake_model):
            self.post([self.event("84")])
        self.assertEqual(self.session()["raw_outcomes"], ["P"])
        card = self.messages[-1][1][0]
        self.assertEqual(card["type"], "flex")
        text = panel_text(card)
        self.assertIn("第 2 局", text)
        self.assertNotIn("觀望", text)
        self.assertIn("建議注數：5 注", text)
        self.assertNotIn("桌號", text)
        self.assertIn("51.0%", text)
        self.assertFalse(self.forwarded)

    def test_guide_and_all_private_shortcuts_have_only_start_end(self):
        guide = self.legacy.guide_panel()
        self.assertEqual(guide["type"], "flex")
        self.assertIn("使用指南", panel_text(guide))
        self.assertEqual(guide["contents"]["body"]["backgroundColor"], "#101F19")
        self.assertNotIn("桌號", panel_text(guide))
        menu = self.call(action=button_data(guide, "開始分析"))[0]
        prompt = self.finish_selection()[0]
        with patch.object(group, "analyze_history", side_effect=self.fake_model):
            result = self.call("84")[0]
        for card in (guide, menu, prompt, result, private.message("測試")):
            actions = [item["action"]["text"] for item in card["quickReply"]["items"]]
            self.assertEqual(actions, ["開始分析", "結束分析"])

    def test_loading_only_for_valid_private_work_and_failure_never_blocks_reply(self):
        self.start()
        loading = self.legacy._start_loading_animation
        with patch.object(group, "analyze_history", side_effect=self.fake_model) as model:
            self.call("10,4")
            loading.assert_not_called()
            loading.side_effect = TimeoutError("loading failed")
            card = self.call("84", event_id="animated")[0]
            self.assertEqual(card["type"], "flex")
            self.assertEqual(self.call("84", event_id="animated"), [])
            loading.assert_called_once_with("U-one", 10)
            model.assert_called_once()
            store.upsert_session("U-one", {"permanent_access": False, "trial_started_at": 1, "access_until": 2})
            self.assertIn("試用已到期", self.call("38")[0]["text"])
            loading.assert_called_once()

    def test_private_5_10_watch_one_clear_previous_units_without_resetting_history(self):
        self.start()
        def grade(history):
            report = self.fake_model(history)
            if len(history) == 2:
                report["research_view"].update(ev_gap_se=.01, ev_gap_snr=3.45)
            elif len(history) == 3:
                report["research_view"]["ess_ratio"] = .29
            return report
        run = self.session()["analysis_run_id"]
        with patch.object(group, "analyze_history", side_effect=grade):
            for units in (5, 10, 0, 5):
                card = self.call("84")[0]
                self.assertIn(f"建議注數：{units} 注", panel_text(card))
                self.assertNotIn("模擬注數", panel_text(card))
                self.assertNotIn("觀望", panel_text(card))
                self.assertEqual("看一局" in panel_text(card), units == 0)
                self.assertEqual(self.session()["screen_last_prediction"]["simulation_tier"], units)
        self.assertEqual(self.session()["raw_outcomes"], list("PPPP"))
        self.assertEqual(self.session()["analysis_run_id"], run)

    def test_real_engine_flex_matches_group_forecast_for_entire_shoe_prefix(self):
        self.start()
        for pair, expected in (("84", "P"), ("莊7閒2", "PB"), ("９９", "PBT")):
            actual = self.call(pair)[0]
            direction, tier, wanted = group._research(list(expected), group.analyze_history)
            self.assertEqual(actual["type"], "flex")
            self.assertEqual(self.session()["private_points_text"], wanted)
            self.assertNotIn("觀望", panel_text(actual))
            self.assertIn(f"建議注數：{tier} 注", panel_text(actual))
            pred = self.session()["screen_last_prediction"]
            self.assertEqual(pred["bbb_soft_shoe"]["report"], group.analyze_history(list(expected)))
            self.assertEqual((pred["direction"], pred["simulation_tier"]), (direction, tier))
            self.assertEqual((pred["formal_action"], pred["bet_amount"]), ("Skip", 0))

    def test_continue_preserves_history_end_clears_and_stale_result_cannot_end_new_run(self):
        self.start()
        with patch.object(group, "analyze_history", side_effect=self.fake_model) as model:
            card = self.call("84")[0]
            before = self.session()
            prompt = self.call("繼續分析")[0]
            self.assertIn("請輸入閒莊點數", panel_text(prompt))
            self.assertEqual(self.session()["raw_outcomes"], before["raw_outcomes"])
            self.assertEqual(self.session()["analysis_run_id"], before["analysis_run_id"])
            model.assert_called_once()
        ended = self.call(action=button_data(card, "結束分析"))[0]
        self.assertIn("本次分析已結束", panel_text(ended))
        self.assertEqual(self.session()["raw_outcomes"], [])
        self.assertFalse(self.session()["analysis_active"])
        self.start()
        new_run = self.session()["analysis_run_id"]
        self.assertIn("已失效", panel_text(self.call(action=button_data(card, "結束分析"))[0]))
        self.assertTrue(self.session()["analysis_active"])
        self.assertEqual(self.session()["analysis_run_id"], new_run)

    def test_stale_or_foreign_menu_does_not_mutate_new_selection(self):
        old = self.call("開始分析")[0]
        new = self.call("開始分析")[0]
        before = self.session()
        self.assertIn("已失效", panel_text(self.call(action=button_data(old, "DG真人"))[0]))
        self.assertEqual(self.session()["private_points_flow"], before["private_points_flow"])
        self.start("U-two")
        other = self.session("U-two")["analysis_run_id"]
        self.assertIn("已失效", panel_text(self.call(action=button_data(new, "DG真人"), uid="U-two")[0]))
        self.assertEqual(self.session("U-two")["analysis_run_id"], other)

    def test_score_before_game_selection_is_not_reinterpreted_as_bankroll(self):
        menu = self.call("開始分析")[0]
        with patch.object(group, "analyze_history") as model:
            self.assertIn("先完成", self.call("84")[0]["text"])
            game = self.call(action=button_data(menu, "DG真人"))[0]
            self.assertIn("先完成", self.call("84")[0]["text"])
            model.assert_not_called()
        self.assertEqual(self.session()["bankroll"], 0)
        self.assertEqual(self.session()["raw_outcomes"], [])
        self.call(action=button_data(game, "百家樂"))
        self.assertTrue(self.session()["point_score_mode"])

    def test_replayed_game_button_cannot_reset_received_scores(self):
        menu = self.call("開始分析")[0]
        game = self.call(action=button_data(menu, "DG真人"))[0]
        data = button_data(game, "百家樂")
        self.call(action=data, event_id="game")
        with patch.object(group, "analyze_history", side_effect=self.fake_model):
            self.call("84")
        run = self.session()["analysis_run_id"]
        self.assertEqual(self.call(action=data, event_id="game"), [])
        self.assertIn("已失效", panel_text(self.call(action=data)[0]))
        self.assertEqual(self.session()["raw_outcomes"], ["P"])
        self.assertEqual(self.session()["analysis_run_id"], run)

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
        first = audit.summarize(path, audit.scope_key("user", "U-one"))["cohorts"][0]
        self.assertEqual((first["wins"], first["skips"]), (1, 0))
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

    def test_always_candidate_preserves_active_session_and_separates_policy_results(self):
        self.start()
        run_id = self.session()["analysis_run_id"]
        with patch.object(group, "analyze_history", side_effect=self.fake_model), \
                patch.object(private, "AUDIT_POLICY", private.POLICY):
            self.call("84\n38\n99")
        self.assertEqual(self.session()["screen_last_prediction"]["simulation_tier"], 5)

        def weak(history):
            result = self.fake_model(history)
            result["research_view"]["ess_ratio"] = .39
            return result

        with patch.object(group, "analyze_history", side_effect=weak):
            reply = panel_text(self.call("38")[0])
        current = self.session()
        self.assertEqual(current["analysis_run_id"], run_id)
        self.assertEqual(current["raw_outcomes"], list("PBTB"))
        self.assertEqual(current["private_points_policy"], private.POLICY)
        self.assertNotIn("觀望", reply)
        self.assertEqual(current["screen_last_prediction"]["simulation_tier"], 5)
        self.assertEqual(current["screen_last_prediction"]["bbb_soft_shoe"]["report"], weak(list("PBTB")))
        with patch.object(group, "analyze_history", side_effect=self.fake_model):
            self.assertIn("建議注數：5 注", panel_text(self.call("38")[0]))
        cohorts = audit.summarize(audit.database_path(self.path), audit.scope_key("user", "U-one"))["cohorts"]
        self.assertEqual(len(cohorts), 2)
        self.assertEqual({c["policy"] for c in cohorts}, {private.POLICY, private.AUDIT_POLICY})
        current = next(c for c in cohorts if c["policy"] == private.AUDIT_POLICY)
        self.assertEqual((current["wins"], current["skips"]), (1, 0))

    def test_nonconsecutive_redelivery_and_old_start_never_duplicate_or_reset(self):
        store.select_venue("U-one", "DG")
        self.call("開始分析", event_id="start")
        self.finish_selection()
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
        self.start()
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
        self.assertEqual(self.session()["private_points_stage"], "venue")
        self.assertFalse(self.session()["point_score_mode"])
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
        self.assertIn("第 2 局", panel_text(self.pushes[0][1][0]))


if __name__ == "__main__":
    unittest.main()
