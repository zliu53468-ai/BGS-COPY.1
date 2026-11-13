"""Prospective model evaluation, isolated from predictions and betting ledgers.

Only a forecast saved in an EARLIER input event can be scored. Imported history
never creates retrospective forecasts. Outcomes are user/OCR observations, not
independently verified casino records; delivery of a forecast is not asserted.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from functools import wraps
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

BASE_PROB = {"B": .458597, "P": .446247, "T": .095156}
COMMANDS = {"查詢成績", "預測成績"}


def database_path(state_path: Path | None = None, environ=None) -> Path:
    env = os.environ if environ is None else environ
    configured = str(env.get("PREDICTION_AUDIT_FILE", "")).strip()
    if configured:
        return Path(configured).expanduser()
    state = state_path or Path(env.get("SESSION_DATA_FILE", "data/sessions.json"))
    return Path(state).with_name("prediction_audit.sqlite3")


def scope_key(kind: str, identifier: str) -> str:
    if kind not in ("group", "user") or not identifier:
        raise ValueError("invalid_audit_scope")
    return sha256(f"{kind}:{identifier}".encode()).hexdigest()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=.25)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1):
        conn.close()
        raise ValueError("unsupported_audit_schema")
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS shoes (
        scope TEXT, shoe TEXT, last_hand INTEGER NOT NULL DEFAULT 0,
        conflicted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(scope, shoe));
      CREATE TABLE IF NOT EXISTS inputs (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, shoe TEXT,
        event_key TEXT, recorded_at REAL, source TEXT,
        UNIQUE(scope, shoe, event_key));
      CREATE TABLE IF NOT EXISTS observations (
        scope TEXT, shoe TEXT, hand INTEGER, outcome TEXT,
        player INTEGER, banker INTEGER, input_seq INTEGER,
        PRIMARY KEY(scope, shoe, hand));
      CREATE TABLE IF NOT EXISTS forecasts (
        scope TEXT, shoe TEXT, hand INTEGER, input_seq INTEGER,
        recorded_at REAL, direction TEXT, model_key TEXT, model_version TEXT,
        policy TEXT, source_commit TEXT, deployment_commit TEXT,
        probabilities TEXT, units INTEGER, evidence TEXT,
        PRIMARY KEY(scope, shoe, hand));
      CREATE INDEX IF NOT EXISTS forecasts_scope ON forecasts(scope, model_key);
      PRAGMA user_version=1;
    """)
    return conn


def record_history(*, path: Path, scope: str, shoe: str, event_key: str,
                   history: list[str], report: Mapping[str, Any], direction: str,
                   policy: str, source: str, source_commit: str = "",
                   units: int = 0, scores=(), offset: int = 0) -> bool:
    """Record accepted input, then freeze its NEXT forecast, in one transaction.

    offset supports the private point session's bounded history buffer. Missing
    outcomes remain unscored. A changed/shortened history quarantines the shoe
    rather than overwriting old labels or silently improving the hit rate.
    """
    if (not scope or not shoe or not event_key or not history or offset < 0
            or any(x not in ("B", "P", "T") for x in history)
            or direction not in ("B", "P", "SKIP")):
        raise ValueError("invalid_audit_input")
    prob = report.get("probabilities") or {}
    if (not all(type(prob.get(side)) in (int, float) and math.isfinite(prob[side])
                and 0 <= prob[side] <= 1 for side in ("B", "P", "T"))
            or abs(sum(prob[side] for side in ("B", "P", "T")) - 1) > 1e-6):
        raise ValueError("invalid_audit_probabilities")
    version = str(report.get("version") or "unknown")
    source_commit = source_commit or str(report.get("source_commit") or "")
    model_key = sha256(_json([version, policy, source_commit, report.get("model"),
                              report.get("config")]).encode()).hexdigest()
    evidence = {key: report.get(key) for key in (
        "candidate", "ev_banker", "ev_player", "research_view", "features48",
        "feature_schema", "config", "model", "particle_ess", "ancestor_diversity",
    )}
    score_map = {int(pair["hand"]): pair for pair in scores}
    for hand, pair in score_map.items():
        if any(type(pair.get(side)) is not int or not 0 <= pair[side] <= 9
               for side in ("player", "banker")):
            raise ValueError("invalid_audit_scores")
        outcome = "P" if pair["player"] > pair["banker"] else "B" if pair["banker"] > pair["player"] else "T"
        if not offset < hand <= offset + len(history) or history[hand - offset - 1] != outcome:
            raise ValueError("audit_scores_outcome_mismatch")
    encoded_prob, encoded_evidence = _json(prob), _json(evidence)
    now = time.time()
    event_hash = sha256(event_key.encode()).hexdigest()
    end = offset + len(history)
    with closing(_connect(Path(path))) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO shoes(scope,shoe) VALUES (?,?)", (scope, shoe))
        state = conn.execute("SELECT * FROM shoes WHERE scope=? AND shoe=?", (scope, shoe)).fetchone()
        if state["conflicted"]:
            return False
        if conn.execute("SELECT 1 FROM inputs WHERE scope=? AND shoe=? AND event_key=?",
                        (scope, shoe, event_hash)).fetchone():
            return False
        rows = conn.execute("SELECT * FROM observations WHERE scope=? AND shoe=? AND hand>? AND hand<=?",
                            (scope, shoe, offset, end)).fetchall()
        conflict = end < state["last_hand"]
        for row in rows:
            pair = score_map.get(row["hand"])
            if row["outcome"] != history[row["hand"] - offset - 1]:
                conflict = True
            if pair and row["player"] is not None and (
                    pair["player"] != row["player"] or pair["banker"] != row["banker"]):
                conflict = True
        if conflict:
            conn.execute("UPDATE shoes SET conflicted=1 WHERE scope=? AND shoe=?", (scope, shoe))
            return False
        cursor = conn.execute("INSERT INTO inputs(scope,shoe,event_key,recorded_at,source) VALUES (?,?,?,?,?)",
                              (scope, shoe, event_hash, now, source))
        seq = cursor.lastrowid
        for index, outcome in enumerate(history, offset + 1):
            pair = score_map.get(index, {})
            conn.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?)",
                         (scope, shoe, index, outcome, pair.get("player"), pair.get("banker"), seq))
        # Unique target hand freezes the first recorded forecast; no replacement
        # when a screen is reopened or the same history is re-evaluated.
        conn.execute("INSERT OR IGNORE INTO forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (scope, shoe, end + 1, seq, now, direction, model_key, version,
                      policy, source_commit, os.getenv("RENDER_GIT_COMMIT", ""),
                      encoded_prob, int(units), encoded_evidence))
        conn.execute("UPDATE shoes SET last_hand=? WHERE scope=? AND shoe=?", (end, scope, shoe))
    return True


def safe_record(*, environ=None, **kwargs) -> bool:
    """Audit storage must never turn a successful prediction into a failure."""
    env = os.environ if environ is None else environ
    if str(env.get("PREDICTION_AUDIT_ENABLED", "1")).strip() != "1":
        return False
    try:
        return record_history(**kwargs)
    except Exception as exc:
        print("prediction_audit_error", type(exc).__name__, flush=True)
        return False


def _net(direction: str, outcome: str) -> float:
    if outcome == "T":
        return 0.
    return (.95 if direction == "B" else 1.) if direction == outcome else -1.


def summarize(path: Path, scope: str) -> dict:
    result = {"cohorts": [], "observations": 0, "conflicted_shoes": 0}
    if not Path(path).exists():
        return result
    with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=.25)) as conn:
        conn.row_factory = sqlite3.Row
        result["observations"] = conn.execute(
            "SELECT COUNT(*) FROM observations o JOIN shoes s USING(scope,shoe) WHERE o.scope=? AND s.conflicted=0",
            (scope,)).fetchone()[0]
        result["conflicted_shoes"] = conn.execute(
            "SELECT COUNT(*) FROM shoes WHERE scope=? AND conflicted=1", (scope,)).fetchone()[0]
        rows = conn.execute("""SELECT f.*, o.outcome FROM forecasts f
          JOIN shoes s USING(scope,shoe)
          LEFT JOIN observations o ON o.scope=f.scope AND o.shoe=f.shoe AND o.hand=f.hand
            AND o.input_seq>f.input_seq
          WHERE f.scope=? AND s.conflicted=0 ORDER BY f.input_seq""", (scope,)).fetchall()
    cohorts = {}
    for row in rows:
        key = row["model_key"]
        if key not in cohorts:
            cohorts[key] = {"model_key": key, "version": row["model_version"], "policy": row["policy"],
                            "forecasts": 0, "resolved": 0, "wins": 0, "losses": 0, "ties": 0,
                            "skips": 0, "unresolved": 0, "net_units": 0., "banker_net_units": 0.,
                            "banker_wins": 0, "brier_sum": 0., "log_loss_sum": 0.,
                            "baseline_brier_sum": 0., "baseline_log_loss_sum": 0.,
                            "max_loss_streak": 0, "_streak": 0,
                            "_last_shoe": None, "_last_hand": None, "last_input_seq": 0,
                            "calibration": defaultdict(lambda: {"count": 0, "wins": 0, "prob_sum": 0.})}
        m = cohorts[key]
        if m["_last_shoe"] != row["shoe"] or m["_last_hand"] != row["hand"] - 1:
            m["_streak"] = 0
        m["_last_shoe"], m["_last_hand"] = row["shoe"], row["hand"]
        m["last_input_seq"] = row["input_seq"]
        m["forecasts"] += 1
        outcome, direction = row["outcome"], row["direction"]
        if outcome is None:
            m["unresolved"] += 1
            m["_streak"] = 0
            continue
        m["resolved"] += 1
        prob = json.loads(row["probabilities"])
        for values, prefix in ((prob, ""), (BASE_PROB, "baseline_")):
            m[prefix + "brier_sum"] += sum((values[s] - int(s == outcome)) ** 2 for s in BASE_PROB)
            m[prefix + "log_loss_sum"] -= math.log(max(1e-15, values[outcome]))
        if direction not in ("B", "P"):
            m["skips"] += 1
            m["_streak"] = 0
            continue
        m["net_units"] += _net(direction, outcome)
        m["banker_net_units"] += _net("B", outcome)
        if outcome == "T":
            m["ties"] += 1
            continue
        won = direction == outcome
        m["wins" if won else "losses"] += 1
        m["banker_wins"] += int(outcome == "B")
        m["_streak"] = 0 if won else m["_streak"] + 1
        m["max_loss_streak"] = max(m["max_loss_streak"], m["_streak"])
        p_win = prob[direction] / max(1e-15, prob["B"] + prob["P"])
        bucket = min(9, int(p_win * 10))
        bucket_data = m["calibration"][str(bucket)]
        bucket_data["count"] += 1
        bucket_data["wins"] += int(won)
        bucket_data["prob_sum"] += p_win
    for m in cohorts.values():
        denominator = m["wins"] + m["losses"]
        m["accuracy"] = m["wins"] / denominator if denominator else None
        m["banker_accuracy"] = m["banker_wins"] / denominator if denominator else None
        for metric in ("brier", "log_loss", "baseline_brier", "baseline_log_loss"):
            total = m.pop(metric + "_sum")
            m[metric] = total / m["resolved"] if m["resolved"] else None
        m.pop("_streak")
        m.pop("_last_shoe")
        m.pop("_last_hand")
        m["calibration"] = dict(m["calibration"])
        result["cohorts"].append(m)
    result["cohorts"].sort(key=lambda m: m["last_input_seq"])
    return result


def report_text(path: Path, scope: str) -> str:
    try:
        data = summarize(path, scope)
    except Exception as exc:
        print("prediction_audit_read_error", type(exc).__name__, flush=True)
        return "⚠️ 成績紀錄暫時無法讀取；預測功能不受影響。"
    lines = ["📊 預測成績（自啟用紀錄後累積）", "僅計算預先保存、之後才輸入結果的模型預測。",
             f"已收錄結果 {data['observations']} 局；歷史資料不算事前預測。"]
    if not data["cohorts"]:
        lines.append("尚無預測紀錄。先正常分析，再輸入後續結果即可自動對帳。")
    for m in data["cohorts"][-3:]:
        name = "總點數模型" if "POINT" in m["policy"] else "Soft Shoe 模型"
        accuracy = "尚無已對帳非和局" if m["accuracy"] is None else f"{m['accuracy']:.2%}"
        lines.extend([f"\n{name}｜版本 {m['model_key'][:8]}",
                      f"已記錄 {m['forecasts']} 筆｜已對帳 {m['resolved']}｜未對帳 {m['unresolved']}",
                      f"命中 {m['wins']}／未中 {m['losses']}／和局 {m['ties']}／無方向 {m['skips']}",
                      f"命中率（排除和局）：{accuracy}",
                      f"固定1注模擬淨值：{m['net_units']:+.2f}｜同批固定莊：{m['banker_net_units']:+.2f}",
                      f"已記錄最長連錯：{m['max_loss_streak']} 次"])
        if m["brier"] is not None:
            lines.append(f"機率誤差：{m['brier']:.4f}｜固定機率基準：{m['baseline_brier']:.4f}（越低越好）")
    if len(data["cohorts"]) > 3:
        lines.append("以上列出最近3個版本；其餘紀錄仍保留。")
    if data["conflicted_shoes"]:
        lines.append(f"⚠️ {data['conflicted_shoes']} 靴有輸入歷史衝突，已排除成績。")
    lines.extend(["\n模擬採莊抽水5%、和局退回；不影響手動注數帳。",
                  "結果來自輸入／OCR；歷史補登不回填預測，未確認訊息送達或實際投注。",
                  "樣本成績不保證未來勝率。"])
    return "\n".join(lines)


def _best_effort(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if os.getenv("PREDICTION_AUDIT_ENABLED", "1").strip() != "1":
            return
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            print("prediction_audit_error", type(exc).__name__, flush=True)
    return wrapped


@_best_effort
def record_private_screen(uid: str, session: Mapping[str, Any], state_path: Path) -> None:
    prediction = session.get("screen_last_prediction") or {}
    soft = prediction.get("bbb_soft_shoe") or {}
    if not soft.get("active"):
        return
    safe_record(path=database_path(state_path), scope=scope_key("user", uid),
                shoe=str(session.get("analysis_run_id") or ""),
                event_key=f"screen:{session.get('screen_data_version')}",
                history=list(session.get("raw_outcomes") or []), report=soft.get("report") or {},
                direction=str(soft.get("display_direction") or "SKIP").upper(),
                policy="PRIVATE_SOFT_SHOE_RESEARCH_V1", source=str(session.get("screen_last_input_source") or "screen"),
                source_commit=str(soft.get("source_commit") or ""))


@_best_effort
def record_private_points(uid: str, session: Mapping[str, Any], state_path: Path) -> None:
    prediction = session.get("screen_last_prediction") or {}
    report = prediction.get("bbb_v26_score") or {}
    scores = session.get("point_score_audit") or []
    offset = int(session.get("point_score_audit_total") or len(scores)) - len(scores)
    history = ["P" if p["player"] > p["banker"] else "B" if p["banker"] > p["player"] else "T" for p in scores]
    safe_record(path=database_path(state_path), scope=scope_key("user", uid),
                shoe=str(session.get("analysis_run_id") or ""),
                event_key=f"points:{session.get('screen_data_version')}", history=history, offset=offset,
                scores=[{"hand": offset + i + 1, **p} for i, p in enumerate(scores)],
                report={**report, "probabilities": (report.get("physical_ev") or {}).get("simulated_probability")},
                direction=str(report.get("direction") or "SKIP").upper(),
                policy="PRIVATE_POINTS_RESEARCH_V1", source="manual_final_score")


def install_store_observer(store_module) -> None:
    """Attach to successful screen commits without changing protected store.py.

    Keep the existing RLock across commit and audit so another input cannot
    overtake its forecast. Original state, return value and errors are preserved.
    """
    original = store_module.update_screen_analysis
    if getattr(original, "_prediction_audit_installed", False):
        return

    @wraps(original)
    def observed(user_id, **kwargs):
        with store_module._LOCK:
            session = original(user_id, **kwargs)
            record_private_screen(str(user_id or "").strip(), session, store_module.SESSION_DATA_FILE)
            return session

    observed._prediction_audit_installed = True
    store_module.update_screen_analysis = observed


def main() -> None:
    """Offline export for later shoe/time-separated evaluation, never training."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--export", type=Path, required=True)
    args = parser.parse_args()
    args.export.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(f"{args.db.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")  # All tables belong to one consistent read snapshot.
        for table in ("shoes", "inputs", "observations", "forecasts"):
            target = args.export / f"{table}.jsonl"
            with target.open("x", encoding="utf-8") as output:
                for row in conn.execute(f"SELECT * FROM {table}"):
                    output.write(_json(dict(row)) + "\n")


if __name__ == "__main__":
    main()
