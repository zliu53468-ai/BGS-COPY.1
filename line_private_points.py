"""Private LINE final totals using the unchanged group Soft Shoe calculation.

Only signed user events may reach this adapter. Membership uses the original
app guard; sessions stay in the existing UID store, never group state.
"""
from __future__ import annotations

import copy
import re
import unicodedata
import uuid
from urllib.parse import parse_qs

import group_text_research as group
import prediction_audit as audit

POLICY = "PRIVATE_POINTS_GROUP_COMPATIBLE_V1"
# Keep active session compatibility; separate results when display policy changes.
AUDIT_POLICY = POLICY + "__" + group.GROUP_GATE_POLICY
START = {"開始分析", "開始點數分析", "點數模式"}
END = {"結束", "結束分析"}
STATUS = {"查詢狀態", "目前狀態"}
RESET = {"🔄 清除重來", "清除路紙", "清除畫面", "重來"}
START_ACTIONS = {"start_screen", "restart_screen", "screen_clear", "reset", "road_clear"}


def _text(event):
    return unicodedata.normalize("NFKC", str((event.get("message") or {}).get("text") or "")).strip()


def _action(event):
    return {key: values[0] for key, values in parse_qs(
        str((event.get("postback") or {}).get("data") or "")).items()}


def supports(legacy, event) -> bool:
    if not hasattr(legacy, "store") or (event.get("source") or {}).get("type") != "user":
        return False
    if event.get("type") == "postback":
        return _action(event).get("action") in START_ACTIONS | {"venue", "end"}
    if event.get("type") != "message" or (event.get("message") or {}).get("type") != "text":
        return False
    raw = _text(event)
    return (raw in START | END | STATUS | RESET | {"點數格式", "繼續分析", "圖片模式"}
            or raw in group.RESULTS or raw.upper().replace(" ", "") in group.RESULT_ALIASES
            or group.parse_group_scores(raw) is not None
            or bool(re.match(r"^(?:[+-]?\d|[閒莊]\s*[:=：]?\s*[+-]?\d)", raw)))


def message(text):
    buttons = [("▶ 開始分析", "開始分析"), ("✍ 點數格式", "點數格式"),
               ("📊 查詢狀態", "查詢狀態"), ("📈 查詢成績", "查詢成績"),
               ("📷 圖片模式", "圖片模式"), ("⏹ 結束分析", "結束分析")]
    return {"type": "text", "text": text[:5000], "quickReply": {"items": [
        {"type": "action", "action": {"type": "message", "label": label, "text": command}}
        for label, command in buttons]}}


def _owned(session):
    return session.get("private_points_policy") == POLICY


def _active(session):
    return _owned(session) and session.get("analysis_active") and session.get("point_score_mode")


def _remember(session, event_id):
    session["private_points_processed"] = (
        list(session.get("private_points_processed") or []) + [event_id])[-group.MAX_EVENTS:]


def _begin(legacy, uid, event_id, query):
    storage = legacy.store
    legacy._ensure_access(uid)
    with storage._LOCK:
        current = storage.get_session(uid)
        if event_id in (current.get("private_points_processed") or []):
            return []
        if query.get("action") == "venue":
            venue = str(query.get("venue") or "").upper()
            if venue not in legacy.VENUE_BY_CODE:
                raise ValueError("無效館別")
            current = storage.select_venue(uid, venue, str(query.get("room") or "1"))
        if not current.get("venue"):
            return [legacy.venue_panel(uid)]
        session = copy.deepcopy(current)
        storage._clear_screen_fields(session)
        session.update({"analysis_mode": "screen", "analysis_active": True,
                        "analysis_run_id": uuid.uuid4().hex[:16],
                        "analysis_started_at": storage._now(), "analysis_ended_at": 0,
                        "point_score_mode": True, "point_score_audit": [], "point_score_audit_total": 0,
                        "last_point_score": {}, "last_point_message_id": "",
                        "awaiting_bankroll": False, "awaiting_screenshot": False,
                        "status": "等待輸入閒莊點數",
                        "private_points_policy": POLICY, "private_points_scores": [],
                        "private_points_text": "", "private_points_processed":
                        list(current.get("private_points_processed") or [])})
        _remember(session, event_id)
        storage.upsert_session(uid, session)
    return [message("✅ 已開始私人點數分析｜目前 0 局\n" + group.SCORE_HELP +
                    "\n每局輸入一次，直接回覆下一局方向與模擬注數。")]


def _evaluate(legacy, uid, event_id, pairs, outcome):
    storage = legacy.store
    operation_lock = legacy._user_image_lock(uid)
    if not operation_lock.acquire(timeout=3):
        raise TimeoutError("上一筆分析尚未完成，請稍後再試。")
    try:
        legacy._ensure_access(uid)
        current = storage.get_session(uid)
        if not _active(current):
            raise RuntimeError("請先輸入「開始分析」，再輸入閒莊點數。")
        if event_id in (current.get("private_points_processed") or []):
            return []
        run_id, version = current["analysis_run_id"], current["screen_data_version"]
        before = list(current.get("raw_outcomes") or [])
        outcomes = (["P" if p["player"] > p["banker"] else "B" if p["banker"] > p["player"] else "T"
                     for p in pairs] if pairs is not None else [outcome])
        history = before + outcomes
        if len(history) > group.MAX_HANDS:
            raise ValueError("本次輸入會超過 72 局上限，整則訊息未記錄；請重新開始分析。")
        if not legacy._PREDICTION_SLOTS.acquire(timeout=5):
            raise TimeoutError("目前分析人數較多，請稍後再試。")
        try:
            report = {}
            direction, tier, text = group._research(history, group.analyze_history, capture=report)
        finally:
            legacy._PREDICTION_SLOTS.release()
        # A restart, end, mode switch or other accepted input invalidates work
        # computed from the earlier snapshot. Never overwrite newer history.
        legacy._ensure_access(uid)
        with storage._LOCK:
            data = storage._load_all_unlocked()
            session = data.get(uid) or {}
            if (not _active(session) or session.get("analysis_run_id") != run_id
                    or session.get("screen_data_version") != version):
                raise RuntimeError("本次分析已結束、重新開始或已更新，舊點數未寫入。")
            if event_id in (session.get("private_points_processed") or []):
                return []
            scores = list(session.get("private_points_scores") or [])
            if pairs is not None:
                scores.extend({"hand": len(before) + i + 1, **p} for i, p in enumerate(pairs))
            session.update(storage._road_state_from_raw(history))
            session.update({"private_points_scores": scores, "private_points_text": text,
                            "point_score_audit": [{"player": p["player"], "banker": p["banker"]} for p in scores],
                            "point_score_audit_total": len(scores),
                            "screen_data_version": version + 1, "screen_prediction_version": version + 1,
                            "screen_last_input_source": "private_final_scores" if pairs is not None else "private_bpt",
                            "screen_last_analyzed_at": storage._now(), "status": "點數分析中",
                            "screen_last_prediction": {"engine": group.VERSION,
                                "bbb_soft_shoe": {"active": True, "report": copy.deepcopy(report),
                                    "display_direction": direction, "source_commit": group.SOURCE_COMMIT},
                                "action": direction, "direction": direction, "formal_action": "Skip",
                                "bet_allowed": False, "bet_amount": 0, "suggested_bet_amount": 0,
                                "simulation_tier": tier}, "updated_at": storage._now()})
            _remember(session, event_id)
            data[uid] = session
            storage._save_all_unlocked(data)
            audit.safe_record(path=audit.database_path(storage.SESSION_DATA_FILE),
                scope=audit.scope_key("user", uid), shoe=run_id, event_key="private:" + event_id,
                history=history, report=report, direction=direction, policy=AUDIT_POLICY,
                source=session["screen_last_input_source"], source_commit=group.SOURCE_COMMIT,
                scores=scores, units=tier)
        return [message(text)]
    finally:
        operation_lock.release()


def handle_event(legacy, event):
    """Return messages/[] when handled, or None to preserve original handling."""
    if not supports(legacy, event):
        return None
    uid = str((event.get("source") or {}).get("userId") or "").strip()
    if not uid:
        return None
    raw, query = _text(event), _action(event)
    # Activation codes keep their original priority, even if a code looks like
    # a valid score. Never consume membership credentials as game history.
    if event.get("type") == "message" and legacy._normalize_access_code(raw) in legacy.ALL_CODES:
        return None
    event_id = str(event.get("webhookEventId") or (event.get("message") or {}).get("id") or "").strip()
    if not event_id:
        return [message("⚠️ 訊息缺少事件 ID，未記錄任何操作。")]
    try:
        action = query.get("action")
        if action in START_ACTIONS - {"start_screen"} and not _active(legacy.store.get_session(uid)):
            return None
        if raw in START or action in START_ACTIONS | {"venue"}:
            return _begin(legacy, uid, event_id, query)
        current = legacy.store.get_session(uid)
        if event_id in (current.get("private_points_processed") or []):
            return []
        if raw == "點數格式":
            return [message(group.SCORE_HELP)]
        if raw == "圖片模式":
            return [legacy._start_screen_flow(uid, new_session=True)["panel"]]
        if not _owned(current):
            return None
        if current.get("analysis_active") and not current.get("point_score_mode"):
            return None  # Original screenshot mode still owns its controls.
        if raw in END or action == "end":
            with legacy.store._LOCK:
                ended = legacy._end_uid_analysis(uid)
                ended.update({"point_score_mode": False, "point_score_audit": [],
                              "point_score_audit_total": 0, "private_points_scores": [],
                              "private_points_text": ""})
                _remember(ended, event_id)
                legacy.store.upsert_session(uid, ended)
            return [message("⏹ 私人分析已結束，本場點數與牌路已清空。\n輸入「開始分析」即可重新開始。")]
        if raw in STATUS:
            status = "分析中" if _active(current) else "已結束或已切換模式"
            return [message(f"📊 私人點數分析：{status}\n已輸入 {len(current.get('raw_outcomes') or [])} 局\n"
                            + (str(current.get("private_points_text") or "") if _active(current) else ""))]
        if raw in RESET and _active(current):
            return _begin(legacy, uid, event_id, query)
        if not _active(current):
            return None
        if raw == "繼續分析":
            return [message(group.SCORE_HELP)]
        pairs = group.parse_group_scores(raw)
        outcome = group.RESULTS.get(group.RESULT_ALIASES.get(raw.upper().replace(" ", ""), raw))
        if pairs is not None or outcome is not None:
            return _evaluate(legacy, uid, event_id, pairs, outcome)
        # Explicit bankroll commands were excluded by supports(); in this mode
        # bare digits are scores, never silently reinterpreted as money.
        return [message("⚠️ 點數格式不正確，整則訊息未記錄。\n" + group.SCORE_HELP)]
    except legacy.AccessExpiredError:
        return [legacy._text("試用已到期，請聯繫管理員開通。")]
    except Exception as exc:
        print("private_points_error", type(exc).__name__, flush=True)
        return [message(f"⚠️ 點數操作未完成：{exc}")]
