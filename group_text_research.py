"""Opt-in, UID-gated LINE group text interface for unchanged BBB Soft Shoe.

This is a research-only simulation ledger. No wagers are made or authorized.
Group and admin allowlists are BOTH mandatory. This module is never called
for one-to-one LINE OCR/LIFF sessions and never changes the BBB engine.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable, Mapping

from bbb_soft_shoe_runtime import MAX_HANDS, VERSION, analyze_history

_LOCK = threading.RLock()
RESULTS = {"紅": "B", "藍": "P", "和": "T",
           "莊": "B", "閒": "P", "和局": "T"}
SIDE_LABEL = {"B": "🔴 莊", "P": "🔵 閒", "T": "🟢 和", "SKIP": "⚪ 觀望"}
ALLOWED_UNITS = (5, 10, 20)
MAX_EVENTS = 200


def _allowed(raw: str) -> set[str]:
    return {part.strip() for part in re.split(r"[,\s]+", raw or "") if part.strip()}


def _path(env: Mapping[str, str]) -> Path:
    explicit = str(env.get("GROUP_STATE_FILE", "")).strip()
    if explicit:
        return Path(explicit).expanduser()
    session_path = str(env.get("SESSION_DATA_FILE", "data/sessions.json"))
    return Path(session_path).expanduser().with_name("group_research_state.json")


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"groups": {}}
    # Fail closed on corrupt storage, rather than silently wiping group records.
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
        raise RuntimeError("群組紀錄格式錯誤，未覆寫既有資料")
    return data


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".bgs-group-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _default() -> dict[str, Any]:
    return {
        "active": False, "paused": False, "history": [],
        "net_units": 0, "pending_tier": 0, "last_research_direction": "SKIP",
        "records": [], "processed": [],
    }


def _status(state: Mapping[str, Any], stop: int) -> str:
    flag = "🛑 暫停休息" if state.get("paused") else (
        "✅ 分析中" if state.get("active") else "⏹ 已結束")
    return (
        "📊 群組研究紀錄\n"
        f"狀態：{flag}\n"
        f"累計：{int(state.get('net_units', 0)):+d} 注（管理員手動紀錄）\n"
        f"休息門檻：{stop} 注\n"
        f"已記錄牌局：{len(state.get('history', []))} 局\n"
        f"上次研究方向：{SIDE_LABEL.get(state.get('last_research_direction'), '觀望')}\n"
        "正式下注：Skip／0 注（未驗證投注優勢）"
    )


def _kelly_simulation(report: Mapping[str, Any], side: str) -> tuple[int, float]:
    """Fractional Kelly exercise using model-estimated B/P odds only.

    Tie is a push. No bankroll is requested: use *fixed 100 reference units*
    only as an abstract classroom scale, never real cash. The visible tier is
    rounded DOWN to 5/10/20; it cannot exceed calculated fractional Kelly.
    Official wager gate is NEVER overridden and all real stakes remain zero.
    """
    if side not in ("B", "P"):
        return 0, 0.0
    prob = report.get("probabilities") or {}
    p = float(prob.get(side) or 0)
    q = float(prob.get("P" if side == "B" else "B") or 0)
    if (not math.isfinite(p) or not math.isfinite(q) or
        min(p, q) < 0 or p + q <= 0 or p + q > 1.0000001):
        return 0, 0.0
    odds = .95 if side == "B" else 1.0
    full_kelly = max(0., (odds * p - q) / (odds * (p + q)))
    fractional = min(1., full_kelly) * .225
    reference_units = math.floor(fractional * 100)
    tier = max((u for u in ALLOWED_UNITS if u <= reference_units), default=0)
    return tier, fractional


def _research(history: list[str], predictor: Callable[[list[str]], Mapping[str, Any]]) -> tuple[str, int, str]:
    report = predictor(history)
    if (report.get("version") != VERSION or report.get("action") != "Skip" or
        report.get("validated_edge") is not False or
        report.get("bankroll_fraction") != 0):
        raise RuntimeError("安全鎖定：BBB 正式下注閘門與研究版本不一致")
    view = report.get("research_view") or {}
    direction = str(view.get("direction") or "SKIP")
    if direction not in ("B", "P") or view.get("eligible") is not True:
        direction = "SKIP"
    road = report.get("road_aux") or {}
    if road.get("validated_betting_edge") is not False:
        raise RuntimeError("安全鎖定：牌路不得授權正式下注")
    tier, fractional = _kelly_simulation(report, direction)
    road_note = ("路同向" if view.get("road_confirmed") else
                 "路分歧" if view.get("road_conflict") else "牌路未確認")
    headline = (
        f"下一局研究：{SIDE_LABEL[direction]}｜{tier} 注"
        if direction in ("B", "P") and tier else "下一局研究：⚪ 觀望｜0 注"
    )
    msg = (
        f"📊 AI 群組分析｜第 {len(history)} 局\n"
        f"{headline}\n"
        f"Kelly 模擬分級：{tier} 注（5／10／20）\n"
        f"牌路輔助：{road_note}｜模擬尺度 {fractional * 100:.2f}%\n"
        "⚠️ 未驗證獲利優勢；非實際投注建議。\n"
        "正式決策：Skip／0 注"
    )
    return direction, tier, msg


def handle_group_event(
    event: Mapping[str, Any], *,
    environ: Mapping[str, str] | None = None,
    predictor: Callable[[list[str]], Mapping[str, Any]] | None = None,
    state_path: Path | None = None,
) -> str | None:
    """Return a text reply for authorized group instructions, else None.

    LINE signature verification must happen in app.py BEFORE calling this.
    No userId => refuse; group-wide "admin" roles are not supplied by LINE
    Messaging API webhooks and must never be inferred from display names.
    """
    env = os.environ if environ is None else environ
    if str(env.get("GROUP_TEXT_ENABLED", "1")).strip() != "1":
        return None
    src = event.get("source") or {}
    if src.get("type") != "group" or event.get("type") != "message":
        return None
    message = event.get("message") or {}
    if message.get("type") != "text":
        return None
    sender = str(src.get("userId") or "").strip()
    group_id = str(src.get("groupId") or "").strip()
    if not sender or not group_id:
        return None
    # A single administrator is configured with GROUP_ADMIN_UID, while
    # GROUP_ADMIN_UIDS remains available for explicit additional admins.
    # Never infer group admin privileges from LINE display name or @bot ID.
    authorized_uids = (
        _allowed(str(env.get("GROUP_ADMIN_UID", "")))
        | _allowed(str(env.get("GROUP_ADMIN_UIDS", "")))
    )
    if sender not in authorized_uids:
        return None
    raw = unicodedata.normalize("NFKC", str(message.get("text") or "")).strip()
    if raw in ("查詢群組ID", "群組ID"):
        # Bootstrap an approved administrator before group ID is allowlisted.
        return f"群組 ID：{group_id}\n請加入 Render GROUP_ALLOWED_IDS；請勿公開此 ID。"
    if group_id not in _allowed(str(env.get("GROUP_ALLOWED_IDS", ""))):
        return None
    stop = -abs(int(env.get("GROUP_STOP_LOSS_UNITS", "30") or "30"))
    if stop > -1 or stop < -100000:
        raise ValueError("invalid_GROUP_STOP_LOSS_UNITS")
    event_id = str(event.get("webhookEventId") or message.get("id") or "").strip()
    if not event_id:
        # Never mutate mutable session state without an idempotency key.
        return "⚠️ 訊息缺少事件 ID，未記錄任何操作。"
    parser = re.fullmatch(r"([+-])\s*(5|10|20)", raw)
    if raw not in RESULTS and raw not in (
        "開始分析", "結束分析", "查詢狀態", "目前狀態", "休息", "暫停分析",
        "查詢指令", "操作說明", "+", "-",
    ) and not parser:
        return None
    path = state_path if state_path is not None else _path(env)
    with _LOCK:
        data = _load(path)
        groups = data["groups"]
        state = groups.setdefault(group_id, _default())
        processed = state.setdefault("processed", [])
        if event_id in processed:
            return None
        # Read-only queries do not need storage or event mutation.
        if raw in ("查詢狀態", "目前狀態"):
            return _status(state, stop)
        if raw in ("查詢指令", "操作說明"):
            return (
                "🔐 群組管理員指令\n"
                "開始分析／結束分析／查詢狀態\n"
                "紅=莊、藍=閒、和=和局\n"
                "+5、-5、+10、-10、+20、-20：手動注數結果\n"
                "+／-：套用上一筆 Kelly 模擬分級\n"
                "休息：主動暫停；達 -30 注自動休息\n"
                "開啟新場次請輸入「開始分析」\n"
                "所有下注級距僅為研究模擬，正式 Skip。"
            )
        if raw == "開始分析":
            groups[group_id] = _default()
            state = groups[group_id]
            state["active"] = True
            result = (
                "✅ 群組分析已開始｜累計 0 注\n"
                "管理員輸入：紅＝莊、藍＝閒、和＝和局\n"
                "回覆下一局研究方向＋Kelly 模擬 0／5／10／20 注。\n"
                "免輸入本金｜正式下注維持 Skip。"
            )
        elif raw == "結束分析":
            state["active"] = False
            state["pending_tier"] = 0
            result = "⏹ 群組分析已結束。\n" + _status(state, stop)
        elif raw in ("休息", "暫停分析"):
            state["paused"] = True
            state["pending_tier"] = 0
            result = "🛑 已暫停研究訊號。\n" + _status(state, stop)
        elif not state.get("active"):
            return "請管理員先輸入「開始分析」。"
        elif raw in RESULTS:
            if state.get("paused"):
                return "🛑 已達休息狀態；不再提供研究配注。新場次請輸入「開始分析」。"
            history = list(state.get("history") or [])
            if len(history) >= MAX_HANDS:
                return "本靴已達 72 局上限；請結束本場，重新開始分析。"
            history.append(RESULTS[raw])
            # Compute before committing the new history: any model failure
            # leaves the state unchanged, with no fabricated prediction.
            direction, tier, analysis = _research(
                history, analyze_history if predictor is None else predictor)
            state["history"] = history
            state["last_research_direction"] = direction
            state["pending_tier"] = tier
            result = (
                f"本局結果：{SIDE_LABEL[RESULTS[raw]]}\n"
                f"{analysis}\n"
                f"累計注數：{int(state.get('net_units', 0)):+d} 注｜-30 注暫停"
            )
        else:
            # A bare +/- settles the last displayed simulated tier.
            amount = int(state.get("pending_tier") or 0) if raw in ("+", "-") else int(parser.group(2))
            sign = (raw[0] if parser else raw)
            if state.get("paused"):
                return "🛑 休息中，模擬注數已停止記錄。"
            if amount not in ALLOWED_UNITS:
                return "目前沒有待結算的模擬級距；請使用 +5／-5 等指令明確記錄。"
            delta = amount if sign == "+" else -amount
            state["net_units"] = int(state.get("net_units") or 0) + delta
            state.setdefault("records", []).append({
                "event_id": event_id, "uid": sender, "delta_units": delta,
                "after_units": state["net_units"],
            })
            state["records"] = state["records"][-MAX_EVENTS:]
            state["pending_tier"] = 0
            if state["net_units"] <= stop:
                state["paused"] = True
                result = (
                    f"📉 本次模擬：{delta:+d} 注｜累計 {state['net_units']:+d} 注\n"
                    f"🛑 累計達 {stop} 注，建議暫停休息；已暫停後續研究。\n"
                    "停止顯示新的研究配注；正式下注依舊 Skip。"
                )
            else:
                result = (
                    f"🧾 管理員紀錄：{delta:+d} 注｜累計 {state['net_units']:+d} 注\n"
                    f"門檻：{stop} 注（手動紀錄，不代表實際投注）"
                )
        state.setdefault("processed", []).append(event_id)
        state["processed"] = state["processed"][-MAX_EVENTS:]
        _save(path, data)
        return result
