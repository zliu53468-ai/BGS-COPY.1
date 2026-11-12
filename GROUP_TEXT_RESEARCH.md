# BGS-COPY.1 — LINE 群組純文字研究模式

**範圍：只增加 LINE 群組文字命令；不修改任何 BBB Soft Shoe 引擎／OCR／LIFF／個人聊天室功能。**

## 安全設計

- LINE Webhook `X-Line-Signature` 先由 `line_group_webhook.py` 使用既有 `app.py` 的驗證函式檢查。禁止放寬 `ALLOW_UNSIGNED_WEBHOOK=0`。
- **雙重白名單**：`GROUP_ADMIN_UIDS`（授權管理員 `U...`）及 `GROUP_ALLOWED_IDS`（允許群組 `C...`）都必須存在且符合，否則群組對話一律不觸發研究、也不回覆私有系統資料。LINE group webhook **不提供可靠的群組管理員角色布林值**；不可用暱稱或群組所有人身份猜測權限。
- 取得 UID：管理員自行私訊 BOT `查詢UID`，將得到的 UID 安全地加入 Render `GROUP_ADMIN_UIDS`。
- 首次取得群組 ID：先填 UID 白名單，把 BOT 邀進群組，授權 UID 在群組輸入 `查詢群組ID`；然後將回覆的 ID 加進 Render `GROUP_ALLOWED_IDS`。在填入群組 ID 前，其餘命令不會啟動。
- 只處理 LINE `source.type=group`；`runtime_app.py` 安裝路由包裝器、`app.py` 保持原始 Git blob；`user` 的個人圖片掃描、會員、LIFF、銀行注碼資料完全走原路徑；未授權的群組訊息及多人的 `room` 事件都不會流入既有個人 UID 路徑。
- 一群一筆持久化狀態。包含 B/P/T 序列、累計*管理員自行回報的模擬注數*、最後一次研究方向、最近的事件 ID、防重複結算、暫停旗標。用單獨 JSON 檔和 `os.replace` 原子寫入；**Render 必須有 Persistent Disk，並僅啟用一個實例／worker**，才能保障目前的單程序鎖。
- 未驗證模型 **正式 action 必須為 `Skip`、`bankroll_fraction=0`、`validated_edge=false`**；任何變更立即失敗並且不更新群組歷史；管理員提供的紅／藍／和只視為結果記錄，並非真實牌面點數。原本 BBB 的 N/L/M、物理 EV、牌路 derived road、研究門檻不變。

## Render Environment 變數

| KEY | VALUE 範例 | 必要性 |
|---|---|---|
| `GROUP_TEXT_ENABLED` | `1` | 建議明確設定。安全白名單為空時不啟用 |
| `GROUP_ADMIN_UIDS` | `U...,...` | 必填，逗號分隔授權管理員 UID |
| `GROUP_ALLOWED_IDS` | `C...,...` | 必填，逗號分隔核准 LINE 群組 ID |
| `GROUP_STOP_LOSS_UNITS` | `30` | 模擬注數累計到 −30（含）自動休息 |
| `GROUP_STATE_FILE` | `/var/data/group_research_state.json` | 持久化磁碟；未設定時使用 `SESSION_DATA_FILE` 同資料夾 |
| `LINE_CHANNEL_ACCESS_TOKEN` | 當前測試 BOT 的新 Token | 既有必填、請勿放在 GitHub |
| `LINE_CHANNEL_SECRET` | 當前測試 BOT 的新 Secret | 既有必填、請勿放在 GitHub |

僅給新 BOT 使用以上群組白名單，不要把另一個正式 LINE BOT 共用的 Token、Secret 或舊群組狀態複製進來。不要公開 Render Deploy Hook、Token、Secret。

## 群組指令

| 文字 | 行為 |
|---|---|
| `開始分析` | 新場次：清空此群組 B/P/T、模擬注數、暫停旗標；不用提供本金 |
| `紅`／`藍`／`和` | 分別記錄 B、P、T，使用**同一個** `bbb_soft_shoe_runtime.analyze_history` 計算研究方向及 ROAD_AUX，直接用 LINE **文字訊息**回覆，不開 LIFF 或 Flex 面板 |
| `+5`／`-5`／`+10`／`-10`／`+20`／`-20` | 管理員記錄一次**模擬**盈虧，不會自動下注；累計到 −30 注即暫停 |
| `+`／`-` | 對**上一筆**研究模擬級距記錄正／負結果；級距為 0 或已使用則須改輸入明確數字 |
| `查詢狀態` | 研究狀態、模擬累計注數、已輸入局數、上一個研究方向 |
| `休息` | 主動暫停新的研究訊號；下個場次須重新輸入 `開始分析` |
| `結束分析` | 關閉本群組場次，保留累計歷史供查詢，等待下個場次 |
| `查詢指令` | 顯示群組指令說明 |
| `查詢UID` | 僅在一對一聊天室回覆自己的 UID |
| `查詢群組ID` | 只有 UID 已授權的管理員，在所屬群組才能取得該群組 ID |

`+` 或 `-` 不需要加入本金。文字指令中的注數為管理員回報記錄，不代表真實投注。重送的 LINE `webhookEventId` 不會被重複記帳。

## Kelly 模擬級距（非正式下注）

- 保留 BBB 對外 `research_view.direction/eligible`，不得以牌路方向覆寫它。
- 若研究方向未達 eligible → 顯示 `⚪ 觀望` 與 `0 注`。
- 以模擬的 B/P 勝負分佈、莊 0.95／閒 1.0、和局視為退回，計算理論 Kelly：`max(0,(b*p_win-p_loss)/(b*(p_win+p_loss)))`。
- 使用純展示的 `fractional=full*0.225` 及**100 單位參考尺度**；往下分級為 0／5／10／20 注，不會向上超過 Kelly 結果，也不會要求實際本金。當預測優勢不足，結果自然為 0。
- **無訓練模型正式下注永遠 `Skip／0 注`。** 此分級只是演算法計算練習，並不構成收益／命中率承諾或實際下注建議。
- 下三路只用來標記同向／分歧，不寫入粒子 likelihood 或調整正式閘門。

## 測試、啟用與回滾

- `python -m unittest -v test_group_text_research.py test_line_group_webhook.py`：管理員 UID + 群組雙白名單、私聊/群組隔離、B/P/T、Kelly 限幅、ID 去重、多群組隔離、-30 注停用、變更正式 Skip 閘門直接拒絕、72 局上限。
- `python -m unittest -v test_bbb_soft_shoe_ocr.py`：既有來源鎖定及 BBB/BGS 各歷史等價。既有整合 OCR 測試有獨立的 `bead_plate_hough_6row_v1` vs `v2_adaptive` 斷言差異，**本變更不得修改 OCR**。
- 群組變更限制在 `group_text_research.py`、`line_group_webhook.py`、`runtime_app.py` 的最小安裝調整、測試、工作流程與說明文件；原本受保護的 `app.py`、OCR、LIFF 等檔案完全不變。
- 變更僅限 `BGS-COPY.1`，原本 `BGS0.3V.1` 或 `BBB` 不會更改。
- **GitHub main 更新不代表 Render 已經部署**：若該服務 Auto-Deploy Off，需手動 Deploy latest commit，先設白名單並檢查 `/prediction-version`、`/health`，再驗證 LINE Webhook、測試指令。
- 一鍵停用此群組功能：Render 設 `GROUP_TEXT_ENABLED=0`，重新部署；個人聊天不受影響。完整程式回滾：使用 `backup/pre-group-text-20261113` 或 revert 本次 merge commit。

請勿把此演算法當成「勝率保證」。管理員輸入的注數僅供手動記帳和風控展示，損益未經第三方核驗。
