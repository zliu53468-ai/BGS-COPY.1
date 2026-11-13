"""Prospective scoring, persistence and isolation; no gambling data fabricated."""
import copy
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import prediction_audit as audit


class AuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "audit.sqlite3"
        self.scope = audit.scope_key("group", "test-group")
        self.report = {"version": "test-v1", "probabilities": {"B": .5, "P": .4, "T": .1},
                       "config": {"particles": 448}, "features48": [0] * 48}
        self.n = 0

    def put(self, history, **kwargs):
        self.n += 1
        values = dict(path=self.path, scope=self.scope, shoe="shoe1", event_key=f"event{self.n}",
                      history=list(history), report=self.report, direction="B", policy="test", source="manual")
        values.update(kwargs)
        return audit.record_history(**values)

    def stats(self):
        return audit.summarize(self.path, self.scope)

    def only(self):
        return self.stats()["cohorts"][0]

    def test_initial_import_is_not_a_backtest_and_batch_only_scores_previous_forecast(self):
        self.put("BPTBP")
        self.assertEqual(self.only()["resolved"], 0)
        self.assertEqual(self.only()["forecasts"], 1)
        self.put("BPTBPBPTB")
        m = self.only()
        self.assertEqual((m["forecasts"], m["resolved"], m["wins"], m["unresolved"]), (2, 1, 1, 1))
        self.assertEqual(self.stats()["observations"], 9)

    def test_realized_metrics_ties_and_commission(self):
        original = copy.deepcopy(self.report)
        self.put("P", direction="B")
        self.put("PB", direction="P")
        self.put("PBP", direction="B")
        self.put("PBPT", direction="P")
        self.put("PBPTB", direction="SKIP")
        m = self.only()
        self.assertEqual((m["resolved"], m["wins"], m["losses"], m["ties"]), (4, 2, 1, 1))
        self.assertAlmostEqual(m["accuracy"], 2 / 3)
        self.assertAlmostEqual(m["banker_accuracy"], 2 / 3)
        self.assertAlmostEqual(m["net_units"], .95)
        self.assertAlmostEqual(m["banker_net_units"], .9)
        outcomes = "BPTB"
        expected = sum(sum((self.report["probabilities"][s] - int(s == o)) ** 2
                           for s in "BPT") for o in outcomes) / 4
        self.assertAlmostEqual(m["brier"], expected)
        self.assertAlmostEqual(m["log_loss"], -sum(math.log(self.report["probabilities"][o]) for o in outcomes) / 4)
        self.assertEqual(sum(b["count"] for b in m["calibration"].values()), 3)
        self.assertEqual(self.report, original)
        self.assertIn("未確認訊息送達", audit.report_text(self.path, self.scope))

    def test_duplicate_event_and_recalculation_cannot_replace_first_forecast(self):
        self.put("B", event_key="same", direction="P")
        self.assertFalse(self.put("BB", event_key="same"))
        self.put("B", direction="B")
        self.put("BP")
        self.assertEqual((self.only()["wins"], self.only()["forecasts"]), (1, 2))

    def test_new_shoe_does_not_resolve_old_pending_hand_or_erase_results(self):
        self.put("B")
        self.put("BB")
        self.put("BB", shoe="shoe2")
        self.assertEqual((self.only()["resolved"], self.only()["unresolved"]), (1, 2))
        self.put("BBB", shoe="shoe2")
        self.assertEqual(self.only()["resolved"], 2)

    def test_changed_history_is_quarantined_instead_of_relabelled(self):
        self.put("B")
        self.put("BB")
        self.assertFalse(self.put("BPP"))
        self.assertEqual(self.stats()["conflicted_shoes"], 1)
        self.assertEqual(self.stats()["cohorts"], [])
        self.assertFalse(self.put("BBB"))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT outcome FROM observations ORDER BY hand").fetchall(), [("B",), ("B",)])

    def test_changed_scores_even_with_same_outcome_quarantine_shoe(self):
        self.put("B", scores=[{"hand": 1, "player": 3, "banker": 8}])
        self.assertFalse(self.put("BP", scores=[{"hand": 1, "player": 3, "banker": 9}]))
        self.assertEqual(self.stats()["conflicted_shoes"], 1)

    def test_wrong_score_label_and_invalid_probability_never_enter_dataset(self):
        with self.assertRaises(ValueError):
            self.put("B", scores=[{"hand": 1, "player": 8, "banker": 3}])
        for prob in ({"B": float("nan"), "P": .4, "T": .1}, {"B": .5, "P": .4, "T": .2}):
            with self.assertRaises(ValueError):
                self.put("B", report={"probabilities": prob})
        self.assertFalse(self.path.exists())

    def test_namespace_and_model_versions_are_separate(self):
        self.put("B")
        self.put("BP", report={**self.report, "version": "test-v2"})
        self.put("BPB", report={**self.report, "version": "test-v2"})
        user = audit.scope_key("user", "test-group")
        self.put("B", scope=user)
        self.assertNotEqual(user, self.scope)
        self.assertEqual([m["resolved"] for m in self.stats()["cohorts"]], [1, 1])
        self.assertEqual(audit.summarize(self.path, user)["cohorts"][0]["resolved"], 0)

    def test_bounded_history_offset_preserves_absolute_hand_numbers(self):
        self.put("B" * 500)
        self.put("B" * 499 + "P", offset=1)
        self.put("B" * 498 + "PB", offset=2)
        self.assertEqual((self.only()["resolved"], self.only()["losses"], self.only()["wins"]), (2, 1, 1))
        self.assertEqual(self.stats()["observations"], 502)

    def test_loss_streak_resets_across_unpredicted_hands_and_shoes(self):
        self.put("B")
        self.put("BP")
        self.put("BPPP")  # hand 3 lost, hand 4 never predicted
        self.put("BPPPP")  # hand 5 lost: new streak
        self.put("B", shoe="new")
        self.put("BP", shoe="new")
        self.assertEqual(self.only()["max_loss_streak"], 2)

    def test_skip_not_a_loss_and_new_input_sequence_required_for_scoring(self):
        self.put("B", direction="SKIP")
        self.put("BP")
        self.assertEqual((self.only()["skips"], self.only()["losses"]), (1, 0))
        self.assertIsNone(self.only()["accuracy"])
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE observations SET input_seq=1 WHERE hand=2")
        self.assertEqual(self.only()["resolved"], 0)

    def test_clock_does_not_control_order(self):
        with patch.object(audit.time, "time", return_value=200):
            self.put("B")
        with patch.object(audit.time, "time", return_value=100):
            self.put("BB")
        self.assertEqual(self.only()["wins"], 1)

    def test_disabled_unwritable_and_malformed_audit_do_not_raise(self):
        with patch.object(audit, "record_history", side_effect=OSError("private-secret")), patch("builtins.print") as log:
            self.assertFalse(audit.safe_record())
            self.assertNotIn("private-secret", str(log.call_args_list))
        with patch.object(audit, "record_history") as record:
            self.assertFalse(audit.safe_record(environ={"PREDICTION_AUDIT_ENABLED": "0"}))
            record.assert_not_called()
        audit.record_private_points("uid", {"point_score_audit": [None]}, self.path)
        audit.record_private_screen("uid", {"screen_last_prediction": "broken"}, self.path)
        self.path.write_text("not sqlite")
        self.assertIn("不受影響", audit.report_text(self.path, self.scope))

    def test_restart_and_consistent_offline_export(self):
        self.put("BP")
        self.put("BPB")
        target = self.path.parent / "export"
        result = subprocess.run([sys.executable, str(Path(audit.__file__)), "--db", str(self.path),
                                 "--export", str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len((target / "forecasts.jsonl").read_text().splitlines()), 2)
        self.assertEqual(len((target / "observations.jsonl").read_text().splitlines()), 3)
        text = (target / "forecasts.jsonl").read_text()
        self.assertNotIn("test-group", text)
        self.assertEqual(json.loads(text.splitlines()[0])["hand"], 3)

    def test_private_screen_commit_and_run_guard_preserve_prediction(self):
        import store
        state_path = self.path.parent / "sessions.json"
        prediction = {"bbb_soft_shoe": {"active": True, "report": self.report,
                                       "display_direction": "P", "source_commit": "test"}}
        with patch.object(store, "SESSION_DATA_FILE", state_path):
            original = store.update_screen_analysis
            self.addCleanup(setattr, store, "update_screen_analysis", original)
            audit.install_store_observer(store)
            installed = store.update_screen_analysis
            audit.install_store_observer(store)
            self.assertIs(store.update_screen_analysis, installed)
            store.select_venue("UID", "DG")
            run = store.begin_screen_analysis("UID")["analysis_run_id"]
            kwargs = dict(ocr={}, detection={}, prediction=prediction, expected_run_id=run)
            first = store.update_screen_analysis("UID", sequence=["B"], raw_outcomes=["B"], **kwargs)
            second = store.update_screen_analysis("UID", sequence=["B", "P"], raw_outcomes=["B", "P"], **kwargs)
            self.assertEqual(first["screen_last_prediction"], prediction)
            self.assertEqual(second["screen_last_prediction"], prediction)
            path, scope = audit.database_path(state_path), audit.scope_key("user", "UID")
            stats = audit.summarize(path, scope)
            self.assertEqual((stats["cohorts"][0]["wins"], stats["cohorts"][0]["forecasts"]), (1, 2))
            store.begin_screen_analysis("UID", clear_existing=True)
            with self.assertRaises(RuntimeError):
                store.update_screen_analysis("UID", sequence=["B", "P", "P"], **kwargs)
            self.assertEqual(audit.summarize(path, scope), stats)


if __name__ == "__main__":
    unittest.main()
