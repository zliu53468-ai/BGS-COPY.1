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
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from bbb_soft_shoe_runtime import MAX_HANDS, SOURCE_COMMIT, VERSION, analyze_history
import prediction_audit as audit

_LOCK = threading.RLock()
RESULTS = {"紅": "B", "藍": "P", "和": "T",
           "莊": "B", "閒": "P", "和局": "T"}
RESULT_ALIASES = {
    "B": "紅", "P": "藍", "T": "和",
    "🔴莊": "紅", "🔵閒": "藍", "🟢和": "和",
}
SIDE_LABEL = {"B": "🔴 莊", "P": "🔵 閒", "T": "🟢 和", "SKIP": "⚪ 觀望"}
ALLOWED_UNITS = (5, 10, 20)
MAX_EVENTS = 200
GROUP_GATE_POLICY = "COPY_BALANCED_DISPLAY_GATE_V1"
GATE_KEYS = (
    "MIN_DIRECTION_ROUNDS", "MIN_DIRECTION_EV_GAP", "MIN_DIRECTION_GAP_SNR",
    "MIN_DIRECTION_ESS_RATIO", "MIN_DIRECTION_DIVERSITY", "MAX_DIRECTION_GAP_SE",
)
# Display-only thresholds; never pass these into the locked prediction engine.
# Keep the core's particle-health limits, but ease its 0.010 EV-gap / 1.05 SNR
# display thresholds. This filters ambiguous estimates, not proven losing bets.
DISPLAY_GATE_THRESHOLDS = {
    "MIN_DIRECTION_ROUNDS": 3,
    "MIN_DIRECTION_EV_GAP": .006,
    "MIN_DIRECTION_GAP_SNR": 1.0,
    "MIN_DIRECTION_ESS_RATIO": .40,
    "MIN_DIRECTION_DIVERSITY": .25,
    "MAX_DIRECTION_GAP_SE": .070,
}
SCORE_HELP = (
    "✍️ 請輸入閒、莊各自最後總點數（0～9）。\n"
    "例如：閒8莊4、8,4 或 84，均為閒8點／莊4點。\n"
    "順序固定先閒後莊；也接受莊4閒8。多局請逐行輸入，最多20局。"
)


def parse_group_scores(value: str) -> list[dict[str, int]] | None:
    """Final totals only; preserve the existing Player-then-Banker convention.

    Pure parsing: never import personal sessions or the independent-score model.
    Each parsed pair becomes one B/P/T observation for the unchanged Soft Shoe.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    chunks = [part.strip() for part in re.split(r"[\n;；]+", text) if part.strip()]
    if not chunks or len(chunks) > 20:
        return None
    pairs = []
    for chunk in chunks:
        pb = re.fullmatch(r"閒\s*[:=：]?\s*([0-9])\s*[,，/、\- ]*莊\s*[:=：]?\s*([0-9])", chunk)
        bp = re.fullmatch(r"莊\s*[:=：]?\s*([0-9])\s*[,，/、\- ]*閒\s*[:=：]?\s*([0-9])", chunk)
        bare = re.fullmatch(r"([0-9])\s*[,，/、\- ]+\s*([0-9])", chunk)
        compact = re.fullmatch(r"([0-9])([0-9])", chunk)
        match = pb or bp or bare or compact
        if not match:
            return None
        left, right = map(int, match.groups())
        player, banker = (right, left) if bp else (left, right)
        pairs.append({"player": player, "banker": banker})
    return pairs


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
        "active": False, "paused": False, "history": [], "score_records": [],
        "run_id": uuid.uuid4().hex,
        "net_units": 0, "pending_tier": 0, "last_research_direction": "SKIP",
        "last_simulation_tier": 0,
        "records": [], "processed": [],
    }


def _fresh_session(old: Mapping[str, Any]) -> dict[str, Any]:
    """Erase shoe/ledger data and rotate run_id, retaining only event de-dupe IDs.

    Processed webhook identifiers are not game history or betting records.
    Keeping a bounded set prevents LINE retries from replaying prior commands.
    """
    clean = _default()
    clean["processed"] = list(old.get("processed") or [])[-MAX_EVENTS:]
    return clean


def _status(state: Mapping[str, Any], stop: int) -> str:
    flag = "🛑 暫停休息" if state.get("paused") else (
        "✅ 分析中" if state.get("active") else "⏹ 已結束")
    return (
        "📊 群組研究紀錄\n"
        f"狀態：{flag}\n"
        f"累計：{int(state.get('net_units', 0)):+d} 注（管理員手動紀錄）\n"
        f"休息門檻：{stop} 注\n"
        f"已記錄牌局：{len(state.get('history', []))} 局\n"
        f"上次研究方向：{SIDE_LABEL.get(state.get('last_research_direction'), '尚未產生') if state.get('history') else '尚未產生'}\n"
        f"上次模擬注數：{int(state.get('last_simulation_tier', state.get('pending_tier', 0)) or 0)} 注\n"
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


def _display_gate(report: Mapping[str, Any], hands: int) -> dict[str, Any]:
    """Select the unchanged candidate or SKIP using existing core diagnostics.

    Invalid/missing predictions are errors, never invented B/P recommendations.
    Missing/invalid quality diagnostics fail closed to observation. No forecast,
    core research eligibility, road signal or formal wager decision is mutated.
    SNR is Monte Carlo separation, NOT a calibrated probability of winning.
    """
    candidate = report.get("candidate")
    ev_b, ev_p = report.get("ev_banker"), report.get("ev_player")
    if (hands < 1 or report.get("observations") != hands
            or candidate not in ("B", "P")
            or not all(type(value) in (int, float) and math.isfinite(value)
                       for value in (ev_b, ev_p))
            or candidate != ("B" if ev_b >= ev_p else "P")):
        raise RuntimeError("模型結果缺失或不一致，本次未產生預測")
    view = report.get("research_view") or {}
    metrics = {key: view.get(key) for key in (
        "abs_ev_gap", "ev_gap_se", "ev_gap_snr", "ess_ratio", "ancestor_diversity",
    )}
    reasons = []
    gate = DISPLAY_GATE_THRESHOLDS
    if hands < gate["MIN_DIRECTION_ROUNDS"]:
        reasons.append("insufficient_rounds")
    valid = all(type(value) in (int, float) and math.isfinite(value) and value >= 0
                for value in metrics.values())
    if not valid:
        reasons.append("invalid_gate_metrics")
    elif (metrics["ess_ratio"] > 1 or metrics["ancestor_diversity"] > 1
          or not math.isclose(metrics["abs_ev_gap"], abs(ev_b - ev_p),
                              rel_tol=1e-6, abs_tol=1e-12)
          or (metrics["ev_gap_se"] > 0 and not math.isclose(
              metrics["ev_gap_snr"], metrics["abs_ev_gap"] / metrics["ev_gap_se"],
              rel_tol=1e-6, abs_tol=1e-12))):
        reasons.append("inconsistent_gate_metrics")
    else:
        if metrics["abs_ev_gap"] < gate["MIN_DIRECTION_EV_GAP"]:
            reasons.append("weak_ev_separation")
        if not 0 < metrics["ev_gap_se"] <= gate["MAX_DIRECTION_GAP_SE"]:
            reasons.append("high_estimator_uncertainty")
        if metrics["ev_gap_snr"] < gate["MIN_DIRECTION_GAP_SNR"]:
            reasons.append("low_gap_to_noise")
        if metrics["ess_ratio"] < gate["MIN_DIRECTION_ESS_RATIO"]:
            reasons.append("low_particle_ess")
        if metrics["ancestor_diversity"] < gate["MIN_DIRECTION_DIVERSITY"]:
            reasons.append("low_particle_diversity")
    return {"direction": "SKIP" if reasons else candidate, "eligible": not reasons,
            "reason": reasons[0] if reasons else "balanced_display_gate_pass",
            "reasons": reasons, "metrics": metrics}


def _display_direction(report: Mapping[str, Any], hands: int) -> str:
    return _display_gate(report, hands)["direction"]


def _research(history: list[str], predictor: Callable[[list[str]], Mapping[str, Any]],
              *, capture: dict | None = None) -> tuple[str, int, str]:
    report = predictor(history)
    if (report.get("version") != VERSION or report.get("action") != "Skip" or
        report.get("validated_edge") is not False or
        report.get("bankroll_fraction") != 0):
        raise RuntimeError("安全鎖定：BBB 正式下注閘門與研究版本不一致")
    core_view = report.get("research_view") or {}
    display_gate = _display_gate(report, len(history))
    direction = display_gate["direction"]
    road = report.get("road_aux") or {}
    if road.get("validated_betting_edge") is not False:
        raise RuntimeError("安全鎖定：牌路不得授權正式下注")
    pair = [(report.get("probabilities") or {}).get(side) for side in ("B", "P")]
    if (not all(type(value) in (int, float) and math.isfinite(value)
                and value >= 0 for value in pair)
            or not 0 < sum(pair) <= 1.0000001):
        raise RuntimeError("模型機率資料無效，未產生固定模擬注數")
    kelly_tier, _fractional = _kelly_simulation(report, direction)
    # Retain the prior simulated tier only when the display gate passes.
    # SKIP must never inherit a prior tier or be promoted by the minimum floor.
    tier = max(ALLOWED_UNITS[0], kelly_tier) if direction != "SKIP" else 0
    fixed_minimum = tier > kelly_tier
    # Keep core forecasts and the formal wager gate intact. The shared
    # display policy is logged separately from the original research decision.
    # This diagnostic logs NO chat ID, UID, screenshot, or personal history.
    if str(os.getenv("GROUP_RESEARCH_GATE_LOG", "1")).strip() == "1":
        print("group_research_gate", json.dumps({
            "repository_commit": os.getenv("RENDER_GIT_COMMIT", "").strip() or None,
            "completed_hands": len(history),
            "model_version": report.get("version"),
            "display_gate_policy": GROUP_GATE_POLICY,
            "core_research_direction": core_view.get("direction", "SKIP"),
            "core_reasons": list(core_view.get("reasons") or []),
            "research_direction": direction,
            "eligible": display_gate["eligible"],
            "core_eligible": core_view.get("eligible") is True,
            "display_basis": "core_candidate_with_quality_gate",
            "reason": display_gate["reason"],
            "reasons": display_gate["reasons"],
            # All metrics are the original core estimates.
            "gate_metrics": display_gate["metrics"],
            "gate_thresholds": dict(DISPLAY_GATE_THRESHOLDS),
            "core_gate_thresholds": {key: (report.get("config") or {}).get(key)
                                     for key in GATE_KEYS},
            "kelly_tier": kelly_tier,
            "display_tier": tier,
            "tier_basis": "observe" if direction == "SKIP" else (
                "fixed_minimum" if fixed_minimum else "kelly"),
        }, ensure_ascii=False, separators=(",", ":")), flush=True)
    direction_label = SIDE_LABEL[direction]
    unit_label = "模擬，固定最低級距" if fixed_minimum else "模擬"
    # Three short lines only. The number is the next hand being forecast;
    # units are research simulations, NEVER real-money stake authorizations.
    msg = (
        f"📊 第 {len(history) + 1} 局\n"
        f"推薦方向：{direction_label}\n"
        f"建議注數：{tier} 注（{unit_label}）"
    )
    if capture is not None:
        capture.update(report)
    return direction, tier, msg


def _record_audit(state, group_id, event_id, report, source, path, env):
    try:
        audit.safe_record(
            path=audit.database_path(path, env), environ=env,
            scope=audit.scope_key("group", group_id), shoe=state["run_id"],
            event_key=event_id, history=state["history"], report=report,
            direction=state["last_research_direction"], units=state["last_simulation_tier"],
            scores=state.get("score_records") or [], policy=GROUP_GATE_POLICY,
            source=source, source_commit=SOURCE_COMMIT)
    except Exception as exc:
        print("prediction_audit_error", type(exc).__name__, flush=True)


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
    raw = RESULT_ALIASES.get(raw.upper().replace(" ", ""), raw)
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
    scores = parse_group_scores(raw)
    if raw not in RESULTS and raw not in (
        "開始分析", "結束分析", "查詢狀態", "目前狀態", "休息", "暫停分析",
        "查詢指令", "操作說明", "點數格式", "查詢成績", "預測成績", "+", "-",
    ) and not parser and scores is None:
        if re.match(r"^(?:[+-]?\d|[閒莊]\s*[:=：]?\s*[+-]?\d)", raw):
            return "⚠️ 點數格式不正確，整則訊息未記錄。\n" + SCORE_HELP
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
        if raw in audit.COMMANDS:
            return audit.report_text(audit.database_path(path, env), audit.scope_key("group", group_id))
        if raw == "點數格式":
            return SCORE_HELP
        if raw in ("查詢指令", "操作說明"):
            return (
                "🔐 群組管理員指令\n"
                "開始分析／結束分析／查詢狀態／查詢成績\n"
                + SCORE_HELP + "\n"
                "紅=莊、藍=閒、和=和局\n"
                "+5、-5、+10、-10、+20、-20：手動注數結果\n"
                "+／-：套用上一筆顯示的模擬注數\n"
                "休息：主動暫停；達 -30 注自動休息\n"
                "「結束分析」會清空本場牌路、模擬注數及待結算紀錄\n"
                "開啟新場次請輸入「開始分析」\n"
                "所有注數級距僅為研究模擬。"
            )
        audit_snapshot = None
        if raw == "開始分析":
            state = groups[group_id] = _fresh_session(state)
            state["active"] = True
            result = (
                "✅ 已開始新一靴｜目前 0 局\n"
                + SCORE_HELP + "\n"
                "每局輸入一次，直接回覆下一局方向與模擬注數。"
            )
        elif raw == "結束分析":
            # Explicitly delete all per-shoe B/P/T history, manual P&L logs,
            # simulated tiers and stop state. A fresh run_id invalidates any
            # screenshot OCR worker that was still processing the prior shoe.
            state = groups[group_id] = _fresh_session(state)
            result = (
                "⏹ 群組分析已結束。\n"
                "🧹 本場牌路、模擬注數、待結算紀錄及暫停狀態已清空。\n"
                "下次請輸入「開始分析」重新建立新一靴。\n"
                + _status(state, stop)
            )
        elif raw in ("休息", "暫停分析"):
            state["paused"] = True
            state["pending_tier"] = 0
            result = "🛑 已暫停研究訊號。\n" + _status(state, stop)
        elif not state.get("active"):
            return "請管理員先輸入「開始分析」。"
        elif raw in RESULTS or scores is not None:
            if state.get("paused"):
                return "🛑 已達休息狀態；不再提供研究配注。新場次請輸入「開始分析」。"
            history = list(state.get("history") or [])
            outcomes = (["P" if pair["player"] > pair["banker"] else
                         "B" if pair["banker"] > pair["player"] else "T"
                         for pair in scores] if scores is not None else [RESULTS[raw]])
            if len(history) + len(outcomes) > MAX_HANDS:
                return "本次輸入會超過 72 局上限，整則訊息未記錄；請結束本場，重新開始分析。"
            previous_hands = len(history)
            history.extend(outcomes)
            # Compute before committing the new history: any model failure
            # leaves the state unchanged, with no fabricated prediction.
            audit_snapshot = {}
            direction, tier, analysis = _research(
                history, analyze_history if predictor is None else predictor, capture=audit_snapshot)
            state["history"] = history
            if scores is not None:
                state.setdefault("score_records", []).extend(
                    {"hand": previous_hands + index + 1, **pair}
                    for index, pair in enumerate(scores))
            state["last_research_direction"] = direction
            state["last_simulation_tier"] = tier
            state["pending_tier"] = tier
            result = analysis
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
        if audit_snapshot is not None:
            _record_audit(state, group_id, event_id, audit_snapshot,
                          "manual_scores" if scores is not None else "manual_bpt", path, env)
        return result
