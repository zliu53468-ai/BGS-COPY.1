# BGS-COPY.1 — LINE 群組純文字研究模式

## 群組與私人聊天同時啟用（2026-11-13）

- 修正私人機器人沒有回應：舊版 `GROUP_ONLY_MODE` 預設為 `1`，會在原私人 handler 執行前直接丟棄私人文字、圖片、加入好友與按鈕事件。現在依 LINE 事件來源分流，兩種聊天室可同時使用。
- `source.type=user` 送回原本私人流程，保留會員授權、館別、本金、圖片、點數模式、按鈕及 UID 專屬狀態；`查詢UID` 仍由查詢捷徑回覆一次。私人功能仍遵循原有啟用條件，不會套用群組白名單或群組的固定方向展示。
- `source.type=group` 只走群組路徑，保留管理員與群組雙重白名單；群組訊息與圖片絕不轉送私人預測。混合 webhook 拆分後各處理一次；`room` 或未知來源不轉入私人流程。
- **`GROUP_ONLY_MODE` 已停用**：即使 Render 留有舊值 `1`，新版也不會因此封鎖私人聊天，可直接刪除該舊變數。無須新增環境設定；需確定部署的是本次修復版本。
- 此次僅修復路由與文件，不修改個人或群組的預測邏輯、模型、總點數輸入、群組固定方向展示、注數或資料內容。


## 現行總點數輸入與固定方向顯示（2026-11-13，取代下方歷史展示規則）

- 開始一靴後，直接輸入**閒、莊各自最後總點數 0～9**。`閒8莊4`、`8,4`、`8/4`、`84`、`８４` 均為閒8／莊4；`莊4閒8` 也接受。未標示名稱時固定**先閒後莊**，不是雙方點數相加。
- 點數只換算當局結果：閒較大為 P、莊較大為 B、相同為 T；然後接續原本同一靴的完整 B/P/T 歷史。Soft Shoe 核心、模型機率、EV、448/80/128、ROAD_AUX 及來源雜湊完全不變。**點數大小不額外加入模型，也不切換到倉庫內另一套獨立點數預測引擎。**
- 展示政策為 `COPY_GROUP_ALWAYS_CANDIDATE_V1`。有效輸入從第1局起直接顯示原模型的 `candidate`（莊或閒），取消群組展示的局數／EV差距／訊噪比／品質觀望篩選。核心 `research_view` 與正式下注 `Skip／0` 原封保留，不把未通過研究門檻的候選方向改標成已驗證優勢；更多方向不代表準確率提高。
- 每次有效預測仍為三行：下一局編號、莊／閒方向、模擬注數。有效方向最低 **5 注**，原 Kelly 級距 10／20 保留；固定補到5時標示「模擬，固定最低級距」。尚未輸入資料時顯示輸入說明；無效機率、缺失／不一致的候選結果或模型失敗會回報錯誤且不寫入資料，絕不隨機補方向。
- 可逐行或用分號一次輸入最多20局。整則訊息驗證成功後，只算最後輸入後的下一局；任一筆無效或整批超過72局上限，全部不記錄。`score_records` 保存對應局號與輸入的總點數，結束或重新開始時清空；這些點數紀錄不傳入模型。
- 原有莊／閒／和、紅／藍／和、B／P／T 和截圖匯入仍接續同一靴；圖片與文字共用 `_research()`，相同牌路必須得到相同方向與模擬注數。快捷列新增「點數格式」，只顯示格式說明，不會送出範例點數或新增牌局。
- 管理員／群組白名單、事件去重、模擬記帳、主動休息與累計 −30 注暫停機制維持原樣。取消觀望不會取消明確的暫停或資料錯誤。
- 文字回覆失敗時仍向原群組單次補送，不重新計算或重記牌局。`group_research_gate` 日誌保留核心原始方向、原因、eligible、門檻及實際展示方向，以 `display_gate_policy`、`display_basis=core_candidate` 區別。沒有展示門檻時 `gate_thresholds={}`，不更改核心的 `core_gate_thresholds`。
- 只修改 BGS-COPY.1 的群組輸入及展示；不修改 BGS0.3V.1、私人預測、OCR、模型檔或正式下注閘門。無須新增 Render 環境變數。

**範圍：LINE 群組命令及共用展示層；不修改任何 BBB Soft Shoe 引擎／OCR／LIFF／個人聊天室功能。下方歷史版本的方向及注數展示規則，以本節為準。**

## 安全設計

- LINE Webhook `X-Line-Signature` 先由 `line_group_webhook.py` 使用既有 `app.py` 的驗證函式檢查。禁止放寬 `ALLOW_UNSIGNED_WEBHOOK=0`。
- **雙重白名單**：`GROUP_ADMIN_UID`（單人）或 `GROUP_ADMIN_UIDS`（多人）與 `GROUP_ALLOWED_IDS`（允許群組 `C...`）都必須存在且符合，否則群組對話一律不觸發研究、也不回覆私有系統資料。LINE group webhook **不提供可靠的群組管理員角色布林值**；不可用暱稱或群組所有人身份猜測權限。
- 取得 UID：管理員自行私訊 BOT `查詢UID`，將得到的 UID 安全地加入 Render `GROUP_ADMIN_UID`。**不要直接寫死在公開 GitHub 原始碼**；可以安全地更換管理員。
- 首次取得群組 ID：先填 `GROUP_ADMIN_UID` 白名單，把 BOT 邀進群組，授權 UID 在群組輸入 `查詢群組ID`；然後將回覆的 ID 加進 Render `GROUP_ALLOWED_IDS`。在填入群組 ID 前，其餘命令不會啟動。
- 只處理 LINE `source.type=group`；`runtime_app.py` 安裝路由包裝器、`app.py` 保持原始 Git blob；`user` 的個人圖片掃描、會員、LIFF、銀行注碼資料完全走原路徑；未授權的群組訊息及多人的 `room` 事件都不會流入既有個人 UID 路徑。
- 一群一筆持久化狀態。包含 B/P/T 序列、累計*管理員自行回報的模擬注數*、最後一次研究方向、最近的事件 ID、防重複結算、暫停旗標。用單獨 JSON 檔和 `os.replace` 原子寫入；**Render 必須有 Persistent Disk，並僅啟用一個實例／worker**，才能保障目前的單程序鎖。
- 未驗證模型 **正式 action 必須為 `Skip`、`bankroll_fraction=0`、`validated_edge=false`**；任何變更立即失敗並且不更新群組歷史；管理員提供的紅／藍／和只視為結果記錄，並非真實牌面點數。原本 BBB 的 N/L/M、物理 EV、牌路 derived road、研究門檻不變。

## Render Environment 變數

| KEY | VALUE 範例 | 必要性 |
|---|---|---|
| `GROUP_TEXT_ENABLED` | `1` | 建議明確設定。安全白名單為空時不啟用 |
| `GROUP_ONLY_MODE` | 已停用 | 舊值 `1`／`0` 均不再封鎖私訊，可刪除；群組與私人事件按來源自動分流 |
| `GROUP_IMAGE_SCAN_ENABLED` | `1` | 開啟**群組圖片上傳 OCR 辨識**（不使用舊個人面板）。只接受授權管理員在允許群組中的截圖 |
| `GROUP_ADMIN_UID` | `U...` | **單一管理員**，只填一個經私訊查詢、核對過的 LINE UID；不要填 LINE Channel ID／Bot basic ID |
| `GROUP_ADMIN_UIDS` | `U...,...` | 可選，額外多名管理員 UID；與 `GROUP_ADMIN_UID` 任選一種或合併使用 |
| `GROUP_ALLOWED_IDS` | `C...,...` | 必填，逗號分隔核准 LINE 群組 ID |
| `GROUP_STOP_LOSS_UNITS` | `30` | 模擬注數累計到 −30（含）自動休息 |
| `GROUP_STATE_FILE` | `/var/data/group_research_state.json` | 持久化磁碟；未設定時使用 `SESSION_DATA_FILE` 同資料夾 |
| `LINE_CHANNEL_ACCESS_TOKEN` | 當前測試 BOT 的新 Token | 既有必填、請勿放在 GitHub |
| `LINE_CHANNEL_SECRET` | 當前測試 BOT 的新 Secret | 既有必填、請勿放在 GitHub |

僅給新 BOT 使用以上群組白名單，不要把另一個正式 LINE BOT 共用的 Token、Secret 或舊群組狀態複製進來。不要公開 Render Deploy Hook、Token、Secret。

## 一般 LINE 群組啟用順序

1. LINE Developers 的此 Channel → Messaging API → 開啟 **Allow bot to join group chats** 與 **Use webhook**；Webhook 指向部署服務的 `/webhook` 並驗證成功。將官方帳號加為好友，再邀入一般群組；同一群組只能有一個 LINE 官方帳號。參考 [LINE 官方群組文件](https://developers.line.biz/en/docs/messaging-api/group-chats/)。
2. 每位操作管理員私訊 BOT `查詢UID`。在 Render 設定 `GROUP_ADMIN_UID=U...`，多人則用 `GROUP_ADMIN_UIDS=U...,U...`，儲存並部署。
3. 已授權管理員在目標群組傳送 `查詢群組ID`，把回覆的 `C...` 加入 `GROUP_ALLOWED_IDS`。確認 `GROUP_TEXT_ENABLED=1`、`GROUP_IMAGE_SCAN_ENABLED=1`，儲存並部署。
4. 群組輸入 `開始分析`，再逐局輸入閒莊總點數，例如 `84`（閒8、莊4）；也可延續既有牌路輸入。每靴只開始一次，重複開始會清空目前牌路。未列入 UID 白名單的成員不能操作，但能看見群組回覆。

這裡的管理員是程式授權的操作人員，不會自動同步 LINE 內的管理員職稱，也不需要把 BOT 設為群組管理員。本程式支援一般群組；LINE 社群 **OpenChat** 不是這條 Messaging API 加群路徑，其內建自動回覆機器人也無法直接執行此 Python／Node 預測服務。若使用 OpenChat，可另開一般操作群組由管理員操作，再手動分享結果到社群。

## 群組指令

| 文字 | 行為 |
|---|---|
| `開始分析` | 新場次：清空此群組 B/P/T、模擬注數、暫停旗標；不用提供本金 |
| 群組上傳 JPG/PNG 牌路截圖 | 透過原本的 `screen_pipeline.analyze_game_screen` 自動辨識完整 B/P/T 序列，再使用原本 Soft Shoe；**不需要本金、不轉私人面板**，結果只回原 LINE 群組 |
| `閒8莊4`／`84`／`8,4` | 記錄閒8、莊4，換算 P 後以原 Soft Shoe 預測下一局 |
| `點數格式` | 顯示先閒後莊的輸入格式，不新增牌局 |
| `紅`／`藍`／`和` | 分別記錄 B、P、T，使用**同一個** `bbb_soft_shoe_runtime.analyze_history` 計算研究方向及 ROAD_AUX，直接用 LINE **文字訊息**回覆，不開 LIFF 或 Flex 面板 |
| `+5`／`-5`／`+10`／`-10`／`+20`／`-20` | 管理員記錄一次**模擬**盈虧，不會自動下注；累計到 −30 注即暫停 |
| `+`／`-` | 對**上一筆**研究模擬級距記錄正／負結果；級距為 0 或已使用則須改輸入明確數字 |
| `查詢狀態` | 研究狀態、模擬累計注數、已輸入局數、上一個研究方向 |
| `休息` | 主動暫停新的研究訊號；下個場次須重新輸入 `開始分析` |
| `結束分析` | **立即清除本群組當場 B/P/T 牌路、模擬注數、手動紀錄、待結算級距、暫停旗標，關閉場次**；下一次 `開始分析` 才重新計數。內部只保留最多 200 筆 Webhook ID 防重送，並旋轉 run_id 讓舊 OCR 結果失效 |
| `查詢指令` | 顯示群組指令說明 |
| `查詢UID` | 僅在一對一聊天室回覆自己的 UID |
| `查詢群組ID` | 只有 UID 已授權的管理員，在所屬群組才能取得該群組 ID |

`+` 或 `-` 不需要加入本金。文字指令中的注數為管理員回報記錄，不代表真實投注。重送的 LINE `webhookEventId` 不會被重複記帳。

## Kelly 模擬級距（非正式下注）

- 保留 BBB 對外 `research_view.direction/eligible`，不得以牌路方向覆寫它。
- 若研究方向未達 eligible → 顯示 `⚪ 觀望` 與 `0 注`。
- **群組顯示修正（不改 BBB 算法）：** 若 `research_view.eligible=true` 且 `research_view.direction=B/P`，即使 Kelly 算完低於 5 注，也如實顯示「下一局研究：莊／閒｜0 注」，不得誤標為觀望；0 注代表模擬分級不足，不代表該方向已經具備正式下注優勢。
- 真正研究門檻未過，仍保留 `SKIP`／觀望／0 注，同時列出 `research_view.reasons`（例如樣本局數不足、預估 EV 差距不足、MC 估計誤差偏高、ESS／多樣性不足）；候選 B/P 若有，額外標示為**未過門檻、不可當建議**。不放寬 Gate、不使用牌路翻轉方向。
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

## 群組文字回覆版 V2（BGS-COPY.1 專用）

- 群組管理員輸入 `紅/藍/和` 後，只在原群組由該 webhook 事件的 **replyToken** 回覆，不以管理員 `userId` 推播私人面板。顯示 `本局結果`、`下一局研究：🔴 莊/🔵 閒/⚪ 觀望｜模擬 N 注`、`Kelly 模擬分級：0/5/10/20 注`、`牌路輔助` 及 `累計注數`。
- 若 Soft Shoe 未有 eligible 正向研究訊號或凱利模擬級距不足 5 注，可能顯示 0 注；**不得為了固定輸出 5/10/20 而虛構正 EV 或改變研究方向**。
- **歷史設定已撤銷：** 舊版 `GROUP_ONLY_MODE=1` 曾封鎖私訊；目前依本文最上方規則，同時支援群組與私人路徑。群組事件仍不會進入私人流程，`app.py` 保持原樣。
- 群組回覆只使用 LINE 事件原始 `replyToken`，不使用 `push(userId)`；若仍出現私人推播，請檢查是否**其他 Render 舊服務**也使用同一 Channel access token，或使用者曾私訊其他預測機器人。兩個服務共用 Channel Token 時需要拆開／停用其中一個，避免雙重回覆。
- **管理員啟用順序**：先私訊 `查詢UID`，Render 設 `GROUP_ADMIN_UID=U...`，讓該 UID 在群組輸入 `查詢群組ID`，將得到的 `C...` 填 `GROUP_ALLOWED_IDS`，部署後再以群組輸入 `開始分析`。沒有這兩項白名單時會靜默拒絕。

## 群組圖片 OCR 版 V3（保留 Soft Shoe、不需要本金）

1. 先設定 `GROUP_TEXT_ENABLED=1`、`GROUP_IMAGE_SCAN_ENABLED=1`、`GROUP_ADMIN_UID`（或 `GROUP_ADMIN_UIDS`）以及 `GROUP_ALLOWED_IDS`。要有 Render Persistent Disk（`GROUP_STATE_FILE` 建議 `/var/data/group_research_state.json`）。
2. 在**已授權 LINE 群組**輸入 `開始分析`。這會開始新一靴，不要求館別、本金或個人會員授權。
3. 管理員把**完整清晰的大路／珠盤路圖片**傳送到該群組；BOT 用圖片事件 messageId 下載內容，經既有 `_prepare_analysis_image` 和 `screen_pipeline.analyze_game_screen` 復原 B/P/T。收到時以原始群組 `replyToken` 回覆「圖片掃描中」，完成後使用 `_push(groupId)` 把結果送回**同一群組**，絕不 `push(userId)` 送私人聊天室。
4. 模型只使用 OCR **可信的完整 B/P/T 歷史**，沒有額外真實牌值、剩餘牌數或 bankroll 參數。不更改 BBB engine、OCR implementation、原有私人 app.py 或正式 Skip / Kelly 安全閘門。模擬 5／10／20 注與先前群組文字規格相同。
5. 新圖片與先前紀錄必須屬於同一靴，且**已記錄歷史必須是新 OCR 歷史的前綴**，只接受新增局數；重複截圖不重複記局。圖片品質不足／重建不完整／歷史不一致／超過 72 局都不更新模型紀錄。結束或重開分析後，過期圖片結果一律丟棄。
6. LINE 重送同一 `webhookEventId` 不重複掃描。下載、掃描及模型推論在背景進行，OCR 出錯直接在群組提示重新上傳，不推私人面板，也不要求本金。
7. `GROUP_IMAGE_SCAN_ENABLED=0` 可單獨停用群組圖片功能，保持文字群組模式不變。其他原本 Web / 私人圖片分析程式檔保留是為了舊路徑相容，**群組模式不會呼叫其本金設置流程**。

**部署注意：** 修改 GitHub/main 不會讓既有 Auto-Deploy Off 的 Render 服務自動上線；需在正確服務手動部署最新 SHA。確認群組欄位與 Token/Secret 有效，且無其他舊版 Render 服務共用相同 LINE Channel，才可驗證實際使用者體驗。


## 結束分析清牌與研究方向顯示修正（2026-11-13）

- 「結束分析」是明確清除**本群組當場牌路**的命令，不再保留前一靴以供查詢。累計模擬注數、逐次 +/− 記錄、Kelly 待結算級距與暫停狀態也一起歸零；該群組狀態改為已結束。其他群組的歷史不受影響。
- 重新生成 `run_id` 讓尚未完成的舊圖片 OCR 不得回寫；保留有上限的已處理 `webhookEventId` 僅為防止 LINE 重送舊指令，不含牌路或模擬輸贏歷史。
- 研究觀望不等於 Kelly 模擬級距為 0；方向是否顯示 B/P 僅依 BBB `research_view.direction + eligible`，Kelly 0/5/10/20 只影響模擬單位，兩者不能混為一談。
- 觀望可能是研究 Gate 真正未通過：例如樣本不足、EV 分離不足、雜訊太高、粒子 ESS／多樣性不足。此版本顯示原因以利診斷，不會為了減少觀望而放寬標準或捏造正 EV；正式 `action=Skip` / `bankroll_fraction=0` / `validated_edge=false` 繼續保持。
- 完整回滾：在 `BGS-COPY.1` 使用 `backup/pre-clear-and-observe-fix-20261113` 對照舊檔，或 revert 本次 PR 合併 commit；需部署新 Commit 到 Render 才會生效。


## 群組下方快捷按鈕與精簡回覆（2026-11-13）

- 只調整 `BGS-COPY.1` 的群組訊息呈現；不修改 Soft Shoe V5.1、ROAD_AUX、實際正式下注 Skip、OCR 辨識器、權限或私人原始 `app.py`。
- 群組訊息附帶 LINE Messaging API 的 **Quick Reply**（iOS/Android）：`▶ 開始分析`、`📷 上傳牌路`（相簿動作）、`📊 查詢狀態`、`🔴 莊`、`🔵 閒`、`🟢 和`、`⏹ 結束分析`。前六項文字命令與原管理員 UID 和群組 ID 白名單完全相同；非管理員點擊不觸發模型。
- 這是**下方快速回覆按鈕，不是永久 Rich Menu**。依 LINE 規則，群組有人發出新訊息後 Quick Reply 可能消失；BOT 下一次回覆時會再次帶出。LINE PC 桌面端可能無法使用快速按鈕，請以手機版驗收。
- 收到群組截圖時，用同群組 replyToken 回覆「⏳ 牌路掃描中」；掃描完成後使用 `_push(groupId)` 把精簡分析及按鈕送回該群組，**不推送到管理員個人 UID**。
- LINE 官方的 `/v2/bot/chat/loading/start` **只支援一對一聊天室，不支援群組**；因此群組不能使用原生 Loading 轉圈動畫。上述「⏳」是群組相容的文字進度提示，非 LINE 官方動畫，不會呼叫不支援的 API。
- 識別下一局：圖片或手動輸入已確認 `history.length=N` 局，群組顯示 **第 N+1 局（下一局）**，並只列 `研究方向：莊／閒／觀望`、`模擬配注：0／5／10／20 注`、`正式下注：Skip／0 注`。真正觀望時，額外只顯示最主要的原因。未過門檻時不能硬給方向；Kelly 不足 5 注時顯示原研究方向但配注 0 注。
- 此精簡訊息省略冗長技術及重複 OCR/統計敘述；原群組 `查詢狀態`、管理員累計 -30 注休息、`結束分析` 清除歷史等功能不變。
- 回滾：比對 `backup/pre-group-quickreply-20261113`；可 revert 本次 PR 合併提交。若 Render Auto-Deploy 關閉，需要手動部署最新 main SHA。


## 2026-11-13：完全對齊 BGS 58a24c5 的群組三行回覆

- GROUP 版核心檔、Node worker、Python adapter、ROAD_AUX 已在 PR #7 與 BGS0.3V.1/main（`58a24c527226efe5a6f156f6c81b1cb6c2ad71c6`）來源雜湊完全相同，N/L/M 固定 448/80/128；本次**不修改**上述核心及 BGS/BBB 倉庫。
- 群組僅顯示 **三行**：`📊 第 N 局`、`推薦方向：🔴 莊／🔵 閒／⚪ 觀望`、`建議注數：0／5／10／20 注（模擬）`。N 為下一局（已辨識歷史數 + 1）；不再另外顯示正式下注 Skip、安全說明或技術原因，這些規則仍存在於內部程式和此文件。
- 莊／閒嚴格依 BGS 原本 `research_view.direction` 與 `eligible`；未通過研究門檻時仍是觀望／0 注，**不會為了顯示方向而採用未過門檻的候選值**。正式下注強制 Skip／0，僅屬模擬配注，不能當作勝率保證。
- 每次模型回覆（包含圖片 OCR 路徑）在 Render Logs 以 `group_research_gate` 記錄已完成局數、模型版本、研究方向、eligible、研究門檻原因與模擬級距，**不包含 UID、群組 ID、牌路全文或圖片**；可設 `GROUP_RESEARCH_GATE_LOG=0` 關閉。
- 如果畫面持續觀望，先確認 Render 執行 COPY 最新提交；再依 Logs 的 `reason` 判別是否是 BGS 原版的 `insufficient_rounds`、`weak_ev_separation`、`low_gap_to_noise` 等門檻生效。原版設定 `MIN_DIRECTION_ROUNDS=3`：只有 0–2 局時觀望是預期結果。不能靠版本字串或直接放寬 gate 保證出方向。
- OCR、群組 UID / Group ACL、快捷按鈕、結束清除、模擬注數計算均不變。原始 BGS 與 BBB main 未修改。
