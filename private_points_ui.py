"""Pure LINE Flex views for private Soft Shoe points; no model or state writes."""
from __future__ import annotations

import math
from urllib.parse import urlencode

BRAND = "BGS AI預測系統"
BG, PANEL, BORDER = "#101F19", "#123426", "#357653"
GOLD, MINT, WHITE = "#F4CE78", "#8CD9CB", "#FFFFFF"
GREEN, RED = "#008F45", "#C92735"


def _text(value, *, color=WHITE, size="sm", bold=False, **kwargs):
    return {"type": "text", "text": str(value), "color": color, "size": size,
            "wrap": True, **({"weight": "bold"} if bold else {}), **kwargs}


def _box(contents, **kwargs):
    return {"type": "box", "layout": "vertical", "contents": contents, **kwargs}


def _button(label, action, *, color=GREEN, **params):
    return {"type": "button", "style": "primary", "height": "sm", "color": color,
            "action": {"type": "postback", "label": label, "displayText": label,
                       "data": urlencode({"action": action, **params})}}


def _card(title, subtitle, contents, buttons=(), *, alt=None):
    bubble = {"type": "bubble", "size": "mega",
              "header": _box([
                  _text(BRAND, color=GOLD, bold=True, align="center"),
                  {"type": "separator", "color": BORDER, "margin": "md"},
                  _text(title, size="xl", bold=True, align="center", margin="md"),
                  _text(subtitle, color=MINT, size="xs", align="center", margin="sm"),
              ], backgroundColor=BG, paddingAll="20px"),
              "body": _box(contents, backgroundColor=BG, paddingAll="16px", spacing="md")}
    if buttons:
        bubble["footer"] = _box(list(buttons), backgroundColor=BG,
                                paddingAll="12px", spacing="sm")
    return {"type": "flex", "altText": (alt or f"{BRAND}｜{title}")[:400], "contents": bubble}


def venue_panel(venues, flow):
    return _card("請選擇遊戲平台", "選擇平台後，繼續選擇遊戲", [
        _button(v.get("name") or v["code"], "points_venue", venue=v["code"], flow=flow)
        for v in venues])


def game_panel(venue_name, venue, flow):
    return _card("請選擇遊戲廳", venue_name, [
        _box([_text("百家樂", size="lg", bold=True, align="center"),
              _text("選擇後即可輸入閒莊點數", color=MINT, align="center", margin="sm")],
             backgroundColor=PANEL, borderColor=BORDER, borderWidth="1px",
             cornerRadius="12px", paddingAll="18px"),
        _button("百家樂", "points_game", game="baccarat", venue=venue, flow=flow),
    ], [_button("重新選擇平台", "points_menu")])


def input_panel(run, hands=0):
    return _card("請輸入閒莊點數", "BACCARAT ANALYSIS", [
        _box([
            _text("先閒、後莊", color=GOLD, bold=True, size="lg"),
            _text("請輸入雙方最後總點數（0～9）。", color=MINT, margin="sm"),
            _text("例如：84", size="xxl", bold=True, margin="md"),
            _text("代表閒 8 點、莊 4 點", color=MINT, margin="sm"),
        ], backgroundColor=PANEL, borderColor=BORDER, borderWidth="1px",
             cornerRadius="12px", paddingAll="16px"),
        _text(f"已記錄 {hands} 局。每局輸入一次，即可查看下一局分析。", color=MINT, size="xs"),
    ], [_button("結束分析", "points_end", color=RED, run=run)],
        alt="請輸入閒莊最後總點數：先閒後莊，例如84＝閒8、莊4。")


def _probability_row(label, probability, color):
    value = f"{probability * 100:.1f}"
    fill = [] if probability == 0 else [_box([], width=f"{value}%", height="6px",
                                             backgroundColor=color, cornerRadius="3px", flex=0)]
    return _box([
        {"type": "box", "layout": "horizontal", "contents": [
            _text(label, color=MINT), _text(f"{value}%", color=color, bold=True, align="end")]},
        {"type": "box", "layout": "horizontal", "height": "6px", "margin": "sm",
         "backgroundColor": "#2C4237", "cornerRadius": "3px", "contents": fill},
    ])


def result_panel(report, direction, units, next_hand, run):
    prob = report.get("probabilities") or {}
    if (direction not in ("B", "P") or
        not all(type(prob.get(s)) in (int, float) and math.isfinite(prob[s])
                and 0 <= prob[s] <= 1 for s in ("B", "P", "T")) or
        abs(sum(prob[s] for s in ("B", "P", "T")) - 1) > 1e-6):
        raise ValueError("模型機率資料無效，未產生結果面板")
    side = {"B": "莊", "P": "閒"}[direction]
    return _card("分析結果", "BACCARAT ANALYSIS", [
        _box([_text("下一局預測", color=GOLD, bold=True),
              _text(f"第 {next_hand} 局", color=GOLD, size="xxl", bold=True, margin="sm"),
              _text("輸入新一局點數後更新分析", color=MINT, size="xs", margin="sm")],
             backgroundColor=PANEL, borderColor=BORDER, borderWidth="1px",
             cornerRadius="12px", paddingAll="16px"),
        _probability_row("莊", prob["B"], GOLD),
        _probability_row("閒", prob["P"], "#7AE4AD"),
        _probability_row("和", prob["T"], "#BCCEC7"),
        {"type": "separator", "color": BORDER},
        _box([_text("✧ 預測方向", color=GOLD, bold=True, align="center"),
              _text(side, size="xxl", bold=True, align="center", margin="md"),
              _text(f"模型估計機率：{prob[direction] * 100:.1f}%", color=MINT,
                    align="center", margin="sm"),
              _text(f"模擬注數：{units} 注", color=GOLD, bold=True, align="center", margin="sm")],
             backgroundColor="#194532", borderColor=BORDER, borderWidth="1px",
             cornerRadius="8px", paddingAll="18px"),
        _text("機率為模型估計，非保證命中率。", color=MINT, size="xxs", align="center"),
    ], [{"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
        _button("繼續分析", "points_continue", run=run),
        _button("結束分析", "points_end", color=RED, run=run),
    ]}], alt=f"第 {next_hand} 局｜預測方向：{side}｜模擬注數：{units} 注")


def ended_panel():
    return _card("本次分析已結束", "當場點數與牌路已清空", [
        _text("重新開始時，請先選擇遊戲平台。", color=MINT, align="center")
    ], [_button("開始分析", "points_menu")])
