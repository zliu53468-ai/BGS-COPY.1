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
import private_points_ui as ui

POLICY = "PRIVATE_POINTS_GROUP_COMPATIBLE_V1"
# Keep active session compatibility; separate results when display policy changes.
AUDIT_POLICY = POLICY + "__" + group.GROUP_GATE_POLICY
START = {"開始分析", "開始點數分析", "點數模式", "開始牌局分析"}
END = {"結束", "結束分析"}
STATUS = {"查詢狀態", "目前狀態"}
RESET = {"🔄 清除重來", "清除路紙", "清除畫面", "重來"}
START_ACTIONS = {"start_screen", "restart_screen", "screen_clear", "reset", "road_clear"}
MENU_ACTIONS = {"points_menu", "start_guide", "venues"}
POINT_ACTIONS = {"points_venue", "points_game", "points_continue", "points_end"}


def _text(event):
    return unicodedata.normalize("NFKC", str((event.get("message") or {}).get("text") or "")).strip()


def _action(event):
    return {key: values[0] for key, values in parse_qs(
        str((event.get("postback") or {}).get("data") or "")).items()}


def supports(legacy, event) -> bool:
    if not hasattr(legacy, "store") or (event.get("source") or {}).get("type") != "user":
        return False
    if event.get("type") == "postback":
        return _action(event).get("action") in START_ACTIONS | MENU_ACTIONS | POINT_ACTIONS | {"venue", "end"}
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
    result = dict(text) if isinstance(text, dict) else {"type": "text", "text": text[:5000]}
    result["quickReply"] = {"items": [
        {"type": "action", "action": {"type": "message", "label": label, "text": command}}
        for label, command in buttons]}
    return result


def _owned(session):
    return session.get("private_points_policy") == POLICY


def _active(session):
    return _owned(session) and session.get("analysis_active") and session.get("point_score_mode")


def _remember(session, event_id):
    session["private_points_processed"] = (
        list(session.get("private_points_processed") or []) + [event_id])[-group.MAX_EVENTS:]


def _venues(legacy):
    return [{"code": code, "name": entry.get("name") or code + "真人"}
            for code, entry in legacy.VENUE_BY_CODE.items()]


def _fresh_points(storage, current, *, active, stage):
    session = copy.deepcopy(current)
    storage._clear_screen_fields(session)
    session.update({"analysis_mode": "screen", "analysis_active": active,
                    "analysis_run_id": uuid.uuid4().hex[:16],
                    "analysis_started_at": storage._now() if active else 0, "analysis_ended_at": 0,
                    "point_score_mode": active, "point_score_audit": [], "point_score_audit_total": 0,
                    "last_point_score": {}, "last_point_message_id": "",
                    "awaiting_bankroll": False, "awaiting_screenshot": False,
                    "status": "等待輸入閒莊點數" if active else "等待選擇遊戲",
                    "private_points_policy": POLICY, "private_points_scores": [],
                    "private_points_text": "", "private_points_stage": stage,
                    "private_points_flow": current.get("private_points_flow") or uuid.uuid4().hex,
                    "private_points_processed": list(current.get("private_points_processed") or [])})
    return session


def _open_menu(legacy, uid, event_id):
    legacy._ensure_access(uid)
    storage = legacy.store
    with storage._LOCK:
        current = storage.get_session(uid)
        if event_id in (current.get("private_points_processed") or []):
            return []
        session = _fresh_points(storage, current, active=False, stage="venue")
        session["private_points_flow"] = uuid.uuid4().hex
        _remember(session, event_id)
        storage.upsert_session(uid, session)
    return [message(ui.venue_panel(_venues(legacy), session["private_points_flow"]))]


def _choose_venue(legacy, uid, event_id, query):
    legacy._ensure_access(uid)
    venue = str(query.get("venue") or "").upper()
    if venue not in legacy.VENUE_BY_CODE:
        raise ValueError("無效館別")
    storage = legacy.store
    with storage._LOCK:
        current = storage.get_session(uid)
        if event_id in (current.get("private_points_processed") or []):
            return []
        if query.get("action") == "points_venue" and (
            query.get("flow") != current.get("private_points_flow") or
            current.get("private_points_stage") != "venue"):
            return [message("此選單已失效，請使用最新選單或輸入「開始分析」。")]
        selected = storage.select_venue(uid, venue, "1")
        session = _fresh_points(storage, selected, active=False, stage="game")
        # A legacy venue button starts a new navigation flow too.
        if query.get("action") == "venue":
            session["private_points_flow"] = uuid.uuid4().hex
        _remember(session, event_id)
        storage.upsert_session(uid, session)
    name = legacy.VENUE_BY_CODE[venue].get("name") or venue + "真人"
    return [message(ui.game_panel(name, venue, session["private_points_flow"]))]


def _choose_game(legacy, uid, event_id, query):
    legacy._ensure_access(uid)
    with legacy.store._LOCK:
        current = legacy.store.get_session(uid)
        if event_id in (current.get("private_points_processed") or []):
            return []
        if (query.get("game") != "baccarat" or query.get("venue") != current.get("venue") or
            query.get("flow") != current.get("private_points_flow") or
            current.get("private_points_stage") != "game"):
            return [message("此遊戲選單已失效，請使用最新選單或輸入「開始分析」。")]
        return _begin(legacy, uid, event_id, query)


def _begin(legacy, uid, event_id, query):
    storage = legacy.store
    legacy._ensure_access(uid)
    with storage._LOCK:
        current = storage.get_session(uid)
        if event_id in (current.get("private_points_processed") or []):
            return []
        if not current.get("venue"):
            return _open_menu(legacy, uid, event_id)
        session = _fresh_points(storage, current, active=True, stage="input")
        _remember(session, event_id)
        storage.upsert_session(uid, session)
    return [message(ui.input_panel(session["analysis_run_id"]))]


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
            panel = ui.result_panel(report, direction, tier, len(history) + 1, run_id)
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
        return [message(panel)]
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
        if raw in START or action in START_ACTIONS | MENU_ACTIONS:
            return _open_menu(legacy, uid, event_id)
        if action in {"venue", "points_venue"}:
            return _choose_venue(legacy, uid, event_id, query)
        if action == "points_game":
            return _choose_game(legacy, uid, event_id, query)
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
        if action in {"points_continue", "points_end"}:
            legacy._ensure_access(uid)
            if not _active(current) or query.get("run") != current.get("analysis_run_id"):
                return [message("此結果面板已失效，請使用最新面板或輸入「開始分析」。")]
        if raw in END or action in {"end", "points_end"}:
            with legacy.store._LOCK:
                latest = legacy.store.get_session(uid)
                if action == "points_end" and (
                    not _active(latest) or query.get("run") != latest.get("analysis_run_id")):
                    return [message("此結果面板已失效，請使用最新面板。")]
                ended = legacy._end_uid_analysis(uid)
                ended.update({"point_score_mode": False, "point_score_audit": [],
                              "point_score_audit_total": 0, "private_points_scores": [],
                              "private_points_text": "", "private_points_stage": "ended"})
                _remember(ended, event_id)
                legacy.store.upsert_session(uid, ended)
            return [message(ui.ended_panel())]
        if raw in STATUS:
            status = "分析中" if _active(current) else "已結束或已切換模式"
            return [message(f"📊 私人點數分析：{status}\n已輸入 {len(current.get('raw_outcomes') or [])} 局\n"
                            + (str(current.get("private_points_text") or "") if _active(current) else ""))]
        if raw in RESET and _active(current):
            return _begin(legacy, uid, event_id, query)
        if not _active(current):
            if current.get("private_points_stage") in {"venue", "game"}:
                return [message("請先完成平台與百家樂選擇，再輸入閒莊點數。")]
            return None
        if raw == "繼續分析" or action == "points_continue":
            legacy._ensure_access(uid)
            return [message(ui.input_panel(current["analysis_run_id"], len(current.get("raw_outcomes") or [])))]
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
