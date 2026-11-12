"""Authorized LINE group image -> existing OCR -> unchanged BBB Soft Shoe.

Group images never pass into personal UID sessions or money-management code.
All group results are delivered to the groupId, never the sender userId.
The screenshot is only an estimated B/P/T history (no real ranks/cards).
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

import group_text_research as group


def _authorized(event: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    source = event.get("source") or {}
    message = event.get("message") or {}
    if (event.get("type") != "message" or message.get("type") != "image"
            or source.get("type") != "group"):
        return False
    if str(env.get("GROUP_TEXT_ENABLED", "1")).strip() != "1":
        return False
    if str(env.get("GROUP_IMAGE_SCAN_ENABLED", "1")).strip() != "1":
        return False
    user_id = str(source.get("userId") or "").strip()
    group_id = str(source.get("groupId") or "").strip()
    admin_uids = (
        group._allowed(str(env.get("GROUP_ADMIN_UID", "")))
        | group._allowed(str(env.get("GROUP_ADMIN_UIDS", "")))
    )
    return bool(
        user_id and group_id and user_id in admin_uids
        and group_id in group._allowed(str(env.get("GROUP_ALLOWED_IDS", "")))
    )


def reserve_image(
    event: Mapping[str, Any], *,
    environ: Mapping[str, str] | None = None,
    state_path: Path | None = None,
) -> dict[str, str] | str | None:
    """Claim a signed event exactly once and return its group-only delivery target.

    Called only AFTER the router has verified LINE's X-Line-Signature.
    Unknown or unauthorized groups/users receive no reply and no work.
    """
    env = os.environ if environ is None else environ
    if not _authorized(event, env):
        return None
    src = event["source"]
    msg = event["message"]
    group_id = str(src["groupId"])
    message_id = str(msg.get("id") or "").strip()
    event_id = str(event.get("webhookEventId") or message_id).strip()
    if not message_id or not event_id:
        return "⚠️ 圖片缺少訊息 ID，請重新上傳。"
    path = state_path if state_path is not None else group._path(env)
    with group._LOCK:
        data = group._load(path)
        state = data.get("groups", {}).get(group_id)
        if not state or not state.get("active"):
            return "請先在群組輸入「開始分析」，再傳完整牌路圖片。"
        if state.get("paused"):
            return "🛑 累計已達休息條件，群組研究已暫停。"
        if event_id in state.setdefault("processed", []):
            return None
        run_id = str(state.get("run_id") or "")
        if not run_id:
            # Older records may predate run_id. It is still safe to begin a
            # new image scan if no active session switch can be identified.
            run_id = uuid.uuid4().hex
            state["run_id"] = run_id
        state["processed"].append(event_id)
        state["processed"] = state["processed"][-group.MAX_EVENTS:]
        group._save(path, data)
        return {
            "group_id": group_id,
            "message_id": message_id,
            "event_id": event_id,
            "run_id": run_id,
        }


def finish_image(
    claim: Mapping[str, str],
    screen: Mapping[str, Any], *,
    environ: Mapping[str, str] | None = None,
    state_path: Path | None = None,
    predictor=None,
) -> str:
    """Accept a *fully reconstructed*, high-quality B/P/T screenshot snapshot.

    Duplicate snapshots do not count as new hands. If the manually entered
    history disagrees with OCR, refuse without inventing or silently merging
    hand outcomes; ask administrator to correct the shoe first.
    """
    env = os.environ if environ is None else environ
    if screen.get("recognition_quality_ok") is not True or screen.get("reconstructed_all") is not True:
        return (
            "⚠️ 圖片牌路辨識未達完整可信標準，未更新分析。\n"
            + str(screen.get("user_message") or "請傳完整清楚的大路／珠盤截圖。")[:300]
        )
    raw = screen.get("raw_outcomes") or screen.get("sequence") or []
    history = [str(x).upper().strip() for x in raw]
    if not history or any(x not in ("B", "P", "T") for x in history):
        return "⚠️ 沒有可靠的紅／藍／和完整序列，未更新；請重新傳送清楚的牌路截圖。"
    if len(history) > group.MAX_HANDS:
        return "⚠️ 截圖超過本模型 72 局上限，未更新；請換新靴後重新開始分析。"
    group_id = str(claim["group_id"])
    path = state_path if state_path is not None else group._path(env)
    with group._LOCK:
        data = group._load(path)
        state = data.get("groups", {}).get(group_id)
        if (not state or not state.get("active")
                or state.get("paused")
                or str(state.get("run_id") or "") != str(claim.get("run_id") or "")):
            return "⚠️ 此圖片所屬分析場次已結束／暫停／重開，已忽略舊結果。"
        old = list(state.get("history") or [])
        if history[:len(old)] != old:
            return (
                f"⚠️ 圖片牌路與目前已記錄 {len(old)} 局不一致，未覆蓋歷史。\n"
                "請管理員核對截圖是否為同一靴；需要換靴請輸入「開始分析」。"
            )
        if len(history) == len(old):
            return f"📷 已辨識完整牌路 {len(history)} 局，與現有紀錄相同；未重複新增。"
        # Existing BBB analyze_history applies unchanged, with formal Skip
        # and no bankroll input. Compute before mutating the group ledger.
        direction, tier, report_text = group._research(
            history, group.analyze_history if predictor is None else predictor)
        state["history"] = history
        state["last_research_direction"] = direction
        state["pending_tier"] = tier
        group._save(path, data)
        return (
            f"📷 群組圖片掃描完成\n"
            f"已辨識：{len(history)} 局｜本次新增：{len(history)-len(old)} 局\n"
            f"{report_text}\n"
            f"累計注數：{int(state.get('net_units', 0)):+d} 注｜-30 注暫停"
        )


async def scan_and_reply_group(legacy_app: Any, claim: Mapping[str, str]) -> None:
    """Background download/OCR; reply destination is strictly groupId.

    Immediately acknowledge the original group replyToken in the webhook,
    then push the completed OCR result to that groupId (NOT userId).
    """
    from screen_pipeline import analyze_game_screen

    tmp: Path | None = None
    prepared: Path | None = None
    group_id = str(claim["group_id"])
    try:
        def compute() -> str:
            nonlocal tmp, prepared
            deadline = time.perf_counter() + 33.0
            tmp = legacy_app._download_line_image(str(claim["message_id"]))
            prepared = legacy_app._prepare_analysis_image(tmp)
            screen = analyze_game_screen(prepared, existing_session={}, deadline=deadline)
            return finish_image(claim, screen)
        result = await asyncio.wait_for(asyncio.to_thread(compute), timeout=43)
    except TimeoutError:
        result = "⚠️ 群組圖片分析逾時，未確認新局數。請裁切完整牌路區域後重傳。"
    except Exception as exc:
        # Never leak API tokens or full traceback to a LINE group.
        print("group_image_error", type(exc).__name__, str(exc)[:200])
        result = "⚠️ 群組圖片辨識失敗，沒有更新分析。請傳送清晰的 JPG／PNG 牌路截圖。"
    finally:
        for path in (prepared, tmp):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
    if not legacy_app._push(group_id, [legacy_app._text(result)], max_retries=1):
        print("group_image_delivery_failed", group_id[-8:])
