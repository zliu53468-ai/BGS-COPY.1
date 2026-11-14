"""End-to-end final totals, transport validation and bounded full-shoe inference."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bbb_soft_shoe_runtime as soft
from bbb_final57_runtime import _node_command

ROOT = Path(__file__).resolve().parent


class TotalObservationTests(unittest.TestCase):
    def test_exact_sampler_and_original_core_parity(self):
        run = subprocess.run(_node_command() + ["test_soft_shoe_scores.cjs"], cwd=ROOT,
                             capture_output=True, text=True, timeout=35)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_same_winners_different_scores_change_actual_posterior(self):
        natural = soft.analyze_history(["T"] * 4, scores=[{"player": 9, "banker": 9}] * 4)
        drawn = soft.analyze_history(["T"] * 4, scores=[{"player": 0, "banker": 0}] * 4)
        self.assertGreater(natural["estimated_remaining_cards"], 392)
        self.assertLess(natural["estimated_remaining_cards"], 400)
        self.assertEqual(drawn["estimated_remaining_cards"], 392)
        self.assertNotEqual(natural["probabilities"], drawn["probabilities"])
        for report in (natural, drawn):
            self.assertEqual(report["version"], soft.SCORE_VERSION)
            self.assertEqual(report["observation_summary"], {"total_hands": 4, "score_hands": 4, "bpt_only_hands": 0})
            self.assertEqual((report["action"], report["bankroll_fraction"], report["validated_edge"]), ("Skip", 0, False))
            self.assertFalse(report["observed_actual_cards"])
            self.assertAlmostEqual(sum(report["probabilities"].values()), 1)

    def test_mixed_known_unknown_totals_and_no_scores_keep_legacy_output(self):
        h = list("PBT")
        self.assertEqual(soft.analyze_history(h), soft.analyze_history(h, scores=[None] * 3))
        report = soft.analyze_history(h, scores=[None, {"player": 3, "banker": 8}, None])
        self.assertEqual(report["observation_summary"], {"total_hands": 3, "score_hands": 1, "bpt_only_hands": 2})

    def test_invalid_totals_rejected_before_worker_and_worker_rejects_direct_payload(self):
        cases = [[], [{"player": 8, "banker": 4}] * 2, [{"player": 2, "banker": 9}],
                 [{"player": 10, "banker": 0}], [{"player": True, "banker": 0}],
                 [{"player": "8", "banker": 4}], [{}], [[8, 4]],
                 [{"player": 8, "banker": 4, "cards": []}]]
        for scores in cases:
            with self.subTest(scores=scores), patch.object(soft.subprocess, "run") as run:
                with self.assertRaises(ValueError): soft.analyze_history(["P"], scores=scores)
                run.assert_not_called()
        run = subprocess.run(_node_command() + ["bbb_engine/worker_soft_shoe.cjs"], cwd=ROOT,
                             input="\n".join(json.dumps({"history": ["P"], "scores": s}) for s in cases + [None]),
                             text=True, capture_output=True, timeout=5)
        responses = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual(len(responses), len(cases) + 1)
        self.assertTrue(all(r["ok"] is False for r in responses))

    def test_source_lock_includes_copy_observation_module(self):
        soft.verify_source()
        self.assertEqual(set(soft.COPY_SOURCE_BLOBS), {"soft_shoe_score_observation.cjs"})

    def test_full_72_hand_legal_shoe_fits_existing_timeout(self):
        # Actual legal draws from one synthetic eight-deck shoe, never a
        # fabricated win-rate benchmark. Includes cumulative score observations.
        script = """
        const core=require('./bbb_engine/soft_shoe_decision_runtime.js');
        let c=[128,32,32,32,32,32,32,32,32,32],x=42,h=[],s=[];
        const rng=()=>{x^=x<<13;x^=x>>>17;x^=x<<5;return(x>>>0)/4294967296;};
        for(let i=0;i<72;i++){const t=core.deal(c,rng);if(!t)throw Error('short shoe');
          c=t.counts;h.push(t.result);s.push({player:t.player,banker:t.banker});}
        console.log(JSON.stringify({h,s}));
        """
        payload = json.loads(subprocess.run(_node_command() + ["-e", script], cwd=ROOT,
                                           text=True, capture_output=True, check=True, timeout=5).stdout)
        start = time.monotonic()
        report = soft.analyze_history(payload["h"], scores=payload["s"])
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, soft.TIMEOUT_SECONDS)
        self.assertEqual(report["observation_summary"]["score_hands"], 72)
        self.assertIn("probabilities", report)
        self.assertEqual((report["action"], report["validated_edge"], report["bankroll_fraction"]), ("Skip", False, 0))
        print(f"COPY_SCORE_72_HAND_SECONDS={elapsed:.3f}")


class TotalObservationInputTests(unittest.TestCase):
    """Exercise real Python -> worker -> JS reports, not a mocked model version."""

    def setUp(self):
        import group_text_research as group
        import line_private_points as private
        import store
        self.group, self.private, self.store = group, private, store
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.group_path = self.directory / "group.json"
        self.env = {"GROUP_TEXT_ENABLED": "1", "GROUP_ADMIN_UID": "U-test",
                    "GROUP_ALLOWED_IDS": "C-test", "GROUP_STOP_LOSS_UNITS": "30"}
        env_patch = patch.dict(os.environ, {"GROUP_RESEARCH_GATE_LOG": "0",
                                           "PREDICTION_AUDIT_FILE": str(self.directory / "audit.sqlite3")})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        storage_patch = patch.object(store, "SESSION_DATA_FILE", self.directory / "private.json")
        storage_patch.start()
        self.addCleanup(storage_patch.stop)

    def event(self, text, event_id, *, private=False):
        source = {"type": "user" if private else "group", "userId": "U-test"}
        if not private:
            source["groupId"] = "C-test"
        return {"type": "message", "webhookEventId": event_id, "source": source,
                "message": {"type": "text", "id": event_id, "text": text}}

    def test_group_real_worker_accumulates_aligned_totals_and_resets(self):
        group = self.group
        captured = []

        def actual_model(history, *, scores=None):
            # Observe the real worker contract while preserving every output.
            report = soft.analyze_history(history, scores=scores)
            captured.append((list(history), scores, report))
            return report

        def call(text, event_id):
            return group.handle_group_event(self.event(text, event_id), environ=self.env,
                                            state_path=self.group_path, predictor=actual_model)

        call("開始分析", "start")
        self.assertIn("第 3 局", call("84\n38", "batch"))
        self.assertIn("第 4 局", call("和", "unknown-tie"))
        self.assertIn("第 5 局", call("99", "known-tie"))
        history, observations, report = captured[-1]
        expected = [{"player": 8, "banker": 4}, {"player": 3, "banker": 8},
                    None, {"player": 9, "banker": 9}]
        self.assertEqual(history, list("PBTT"))
        self.assertEqual(observations, expected)
        self.assertEqual(report["version"], soft.SCORE_VERSION)
        self.assertEqual(report["observation_summary"],
                         {"total_hands": 4, "score_hands": 3, "bpt_only_hands": 1})
        state = json.loads(self.group_path.read_text())["groups"]["C-test"]
        self.assertEqual(state["history"], history)
        self.assertEqual([record["hand"] for record in state["score_records"]], [1, 2, 4])
        self.assertIsNone(call("99", "known-tie"))
        self.assertEqual(len(captured), 3)
        call("開始分析", "reset")
        self.assertIn("第 2 局", call("99", "after-reset"))
        self.assertEqual(captured[-1][0], ["T"])
        self.assertEqual(captured[-1][1], [{"player": 9, "banker": 9}])
        for _, _, output in captured:
            self.assertEqual((output["action"], output["validated_edge"], output["bankroll_fraction"]),
                             ("Skip", False, 0))

    def test_private_real_worker_keeps_scores_across_unknown_hand_batch_and_restart(self):
        private, store, group = self.private, self.store, self.group
        operation_lock = threading.Lock()
        legacy = SimpleNamespace(
            store=store, ALL_CODES=set(), _normalize_access_code=lambda text: text,
            _ensure_access=lambda uid: None, _user_image_lock=lambda uid: operation_lock,
            _PREDICTION_SLOTS=threading.Semaphore(1),
            AccessExpiredError=type("AccessExpiredError", (Exception,), {}))
        session = private._fresh_points(store, store.get_session("U-test"), active=True, stage="input")
        session["venue"] = "DG"
        store.upsert_session("U-test", session)

        def call(text, event_id):
            return private.handle_event(legacy, self.event(text, event_id, private=True))

        for text, event_id in [("84", "first"), ("和", "unknown"), ("38\n99", "batch")]:
            messages = call(text, event_id)
            self.assertEqual(messages[0]["type"], "flex", messages)
        session = store.get_session("U-test")
        self.assertEqual(session["raw_outcomes"], list("PTBT"))
        self.assertEqual([record["hand"] for record in session["private_points_scores"]], [1, 3, 4])
        aligned = group.scores_for_history(session["raw_outcomes"], session["private_points_scores"])
        self.assertEqual(aligned, [{"player": 8, "banker": 4}, None,
                                   {"player": 3, "banker": 8}, {"player": 9, "banker": 9}])
        prediction = session["screen_last_prediction"]
        report = prediction["bbb_soft_shoe"]["report"]
        self.assertEqual(report, soft.analyze_history(list("PTBT"), scores=aligned))
        self.assertEqual(prediction["engine"], soft.SCORE_VERSION)
        self.assertEqual((prediction["formal_action"], prediction["bet_allowed"], prediction["bet_amount"]),
                         ("Skip", False, 0))
        self.assertEqual((report["action"], report["validated_edge"], report["bankroll_fraction"]),
                         ("Skip", False, 0))
        self.assertEqual(call("38\n99", "batch"), [])
        self.assertEqual(store.get_session("U-test")["raw_outcomes"], list("PTBT"))
        call("重來", "restart")
        reset = store.get_session("U-test")
        self.assertEqual(reset["raw_outcomes"], [])
        self.assertEqual(reset["private_points_scores"], [])
        self.assertNotEqual(reset["analysis_run_id"], session["analysis_run_id"])
        self.assertEqual(call("00", "after-restart")[0]["type"], "flex")
        fresh_report = store.get_session("U-test")["screen_last_prediction"]["bbb_soft_shoe"]["report"]
        self.assertEqual(fresh_report["observation_summary"],
                         {"total_hands": 1, "score_hands": 1, "bpt_only_hands": 0})
        self.assertEqual(fresh_report["estimated_remaining_cards"], 410)


if __name__ == "__main__":
    unittest.main()
