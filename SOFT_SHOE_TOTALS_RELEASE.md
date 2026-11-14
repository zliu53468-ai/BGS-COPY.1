# Soft Shoe：以每局閒／莊總點數條件化觀測

基準：BGS-COPY.1 `554f89f84e41a73de34e270cb189404524aa8f29`。

## 問題與本次範圍

原本私人／群組輸入的最終點數已保存，但呼叫 Soft Shoe 時只傳 B/P/T。
例如 `84`、`閒8莊4`、`8,4` 都只變成一筆 `P`，粒子更新因此無法利用
「閒 8、莊 4」的限制。本次把已保存的總點數送到 JS，直接限制每局
合法發牌路徑，並把該路徑實際消耗的牌值從粒子中扣除。

這是總點數觀測，不代表已知道真實牌面。牌組仍是可能剩餘牌組的後驗近似。
新增總點數條件不保證勝率、EV gap 或 SNR 提高，也不保證每局顯示方向。

## 介面與資料結構

沿用原本的稀疏持久化紀錄，每筆以 1 起算的 `hand` 對應完整歷史，包含和局：

```json
[
  {"hand": 1, "player": 8, "banker": 4},
  {"hand": 3, "player": 6, "banker": 6}
]
```

Python adapter 新介面：

```python
analyze_history(
    ["P", "B", "T"],
    scores=[{"player": 8, "banker": 4}, None, {"player": 6, "banker": 6}],
)
```

對應 worker JSON：

```json
{
  "history": ["P", "B", "T"],
  "scores": [
    {"player": 8, "banker": 4},
    null,
    {"player": 6, "banker": 6}
  ]
}
```

- `scores` 與 `history` 長度完全一致，位置一對一對齊。
- 點數是 0～9 的整數，不接受字串、布林、額外欄位或與 B/P/T 不符的組合。
- 舊局數沒有總點數時，使用 `null`；不會從勝負或截圖虛構總點數。
- 未傳 `scores` 或全部是 `null` 時，走原本 B/P/T 引擎並保持原報告。
- 一旦有已知總點數，回傳版本為 `BBB_SOFT_SHOE_TOTALS_EV_V1`，
  `observation_model` 為 `final_totals_importance_v1`。
- `observation_summary` 記錄 `total_hands`、`score_hands`、`bpt_only_hands`，
  讓稽核可辨識總點數是否真的參與預測。

## 修改檔案與函式

| 檔案 | 修改點 |
| --- | --- |
| `line_private_points.py` | `_evaluate()` 在推算前合併本次與先前的 `private_points_scores`，按局號對齊後傳給 `_research()`；成功才提交狀態；儲存實際報告版本。 |
| `group_text_research.py` | `parse_group_scores()` 繼續保留點數；新增 `scores_for_history()` 對齊稀疏紀錄；`_research()` 傳送並嚴格檢查對應引擎版本；`handle_group_event()` 在推算前準備累積點數。 |
| `bbb_soft_shoe_runtime.py` | 新增 `validate_scores()`；`analyze_history(history, *, scores=None)` 傳送雙序列並驗證觀測摘要、版本、原始碼 hash、安全閘門。 |
| `bbb_engine/worker_soft_shoe.cjs` | `evaluate()` 驗證 JSON，依是否有已知點數選擇原引擎或總點數觀測模組，維持原安全檢查。 |
| `bbb_engine/soft_shoe_score_observation.cjs` | 新模組；`branchesFor()` 建立合法點數分支，`propose()` 產生並加權合法發牌路徑，`posterior()` 累積更新粒子，`run()` 接回原預測流程。 |
| `bbb_engine/soft_shoe_decision_runtime.js` | 僅在 `run()` 增加可選的第六參數 `observationPosterior`，允許替換後驗來源，並使用該後驗的 seed；原有後續邏輯保留。 |
| `group_image_scan.py` | `finish_image()` 經共用入口分析時，保留已有人工總點數；新增 OCR 局數仍只有 B/P/T。 |
| `prediction_audit.py` | `record_history()` 加記觀測模型與摘要；不同報告版本維持獨立稽核群組。 |

沒有修改其他倉庫。未改動下一手發牌模擬、EV 公式、模型權重、ROAD_AUX、
臨界自適應模擬、多步路徑、顯示門檻或私人卡片 UI。

## JS 條件化方式

1. 每顆粒子保存 10 種百家樂牌值的剩餘數量，初始為原八副牌設定。
2. 每局列出與觀測總點數相容的前兩張合計與補牌分支。
   任一方天然 8／9 時雙方停止補牌；其他情況遵守原有閒／莊補牌規則。
   最終 8／9 也可能來自補牌，因此不直接推斷為天然牌。
3. 從分支中抽樣，再按粒子的實際剩餘牌數抽取 P1、B1。
   P2、B2 及必要的第三張由目標點數與補牌規則決定；缺牌即拒絕。
4. 接受的路徑必須精確產生觀測的兩個總點數。扣牌順序保持
   P1、B1、P2、B2、必要時 P3、B3，抽牌不放回。
5. 使用重要性修正，避免把所有合法路徑當作等機率。
   若共有 K 個候選分支，路徑權重為 `p(path) / q(path)`，等於
   `K × 所有被條件指定之牌的實際抽中機率乘積`。
6. 每顆粒子用 96 次候選的平均加權質量更新原權重；原本的軟深度條件
   仍保留。從候選池依權重選取下一個剩餘牌組。
7. ESS 低於原門檻或有零權重粒子時，進行系統重採樣。
   這會移除不再支持完整觀測歷史的粒子槽位；每個子粒子仍取自合法路徑池。
8. 全部候選失效時，回傳 `score_particle_collapse`。
   已知總點數不會在失敗時被忽略，也不會退回 B/P/T-only 預測。

新模組提供後驗粒子與權重給原核心，後續下一手模擬、EV、派生路與安全閘門
繼續使用原實作。僅在 Python 先推算點數再餵 B/P/T，無法把條件化後的粒子
傳入原 JS；因此採用雙序列輸入與最小後驗入口。

原本軟深度先驗仍會影響權重，所以此模型是保留既有深度假設的有限粒子近似。
原核心 `estimated_remaining_cards` 延用粒子未加權平均，屬估計診斷值，
不是實際觀測到的剩餘張數；本次未改其原有定義。

## 多局與失敗處理

每次請求送出同一靴截至目前的完整歷史與對齊點數。JS 由第一局依序重播更新，
第 n 局接續第 n−1 局的粒子牌組與權重，不是只限制最後一局。
亂數 seed 包含觀測模型版本、歷史及點數，同樣輸入可重現；不使用 LINE UID。

批次輸入、和局、混合未知點數、重複事件去重與重新開始皆沿用既有狀態流程。
推算、版本驗證或合法機率失敗時，輸入不會提交到當前靴。
本次未增加獨立的撤回指令或更改權限流程。

## 保留設定

```text
PARTICLE_COUNT = 512
LIKELIHOOD_DRAWS = 96
NEXT_HAND_DRAWS = 192
MAX_HANDS = 72
TIMEOUT_SECONDS = 8

action = "Skip"
validated_edge = false
bankroll_fraction = 0
observed_actual_cards = false
```

原正式下注閘門仍保留；worker 與 Python adapter 會拒絕違反上述下注鎖定的報告。
既有「看一局／5／10」顯示規則保留，屬原研究顯示流程。

## 驗證

```bash
python -m unittest -v test_soft_shoe_scores test_group_text_research test_line_private_points test_group_image_scan test_line_group_webhook test_prediction_audit test_private_points_ui test_group_reply_ui test_bbb_soft_shoe_ocr
```

本次共驗證 143 項相關 Python 測試：142 項於整合批次通過；原本就模擬 OCR
輸出的群組路由測試，修正測試匯入隔離後單獨補跑通過。該修正只避免路由測試
在模擬辨識結果時仍強制載入 OpenCV，未變更實際 OCR。
72 局總點數的 Python→worker→JS 推算在本次環境約 1.70 秒，低於原 8 秒上限。

新增測試涵蓋：

- 100 種總點數的合法路徑重播。
- 兩個小型耗損牌組的窮舉比對：觀測似然與剩餘各牌值的一階矩。
- 還原原核心並核對基準 git blob，再比較完整 B/P/T 報告。
- 同樣勝負、不同點數會改變實際扣牌與後驗；四局 0／0 必然消耗 24 張牌。
- 多局累積、私人／群組實際 worker 串接、和局、未知舊點數與重啟。
- 拒絕已知總點數被降級成舊版報告，並維持失敗不寫入。
- 68 局 0／0 後剩 8 張；69 局後不足以預測下一手；70 局觀測使粒子崩潰。
- 72 局合法合成牌靴可在原 timeout 內完成；此測試不代表實戰勝率驗證。

完整 OCR 辨識依賴原專案的 OpenCV 等套件；它們未因本次改動而修改。
程式碼提交與線上服務部署是不同狀態，需以部署平台實際載入的 commit 確認生效。
