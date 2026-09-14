# GUI／擴充功能共用契約

日期：2026-09-13。對應 [追蹤計畫](UI_SYNC_REDESIGN_PLAN.md) A-01 至 A-04。

本文件記錄本次實作的整合界線；驗收證據另記錄於 `docs/implementation/ui-sync-redesign/`。

## 資料與協定

- 後端是模型目錄與服務設定的權威來源；擴充功能保存選擇、介面偏好及有來源識別的目錄快取。
- 新版以 `/asr` 上的 `desktop_catalog` v2 取得 STT 和翻譯目錄；舊 `translation_catalog` v1 保留相容，不將 v1 宣稱為完整同步。
- `server_id` 識別持久的後端設定，`instance_id` 識別本次程序；URL 是連線位置。三者用途不同。
- `revision` 用於設定衝突與刷新判斷。可用性另外反映實際模型檔案，不能只因 revision 沒變就忽略檔案遺失。
- 清單與 readiness 查詢不觸發下載、模型載入或翻譯使用權。

請求：

```json
{"type":"desktop_catalog","version":2}
```

回應主要欄位：

```text
type: "desktop_catalog"
version: 2
server_id: persistent server identity
instance_id: current backend process identity
revision: saved configuration revision
models: [{id, name, engine: "desktop", selection: {kind: "local" | "profile", id}, available, status, reason?}]
stt_models: [{id, name, available, status, selection: {model, backend?}}]
defaults: {stt: {model, backend?}, translation: {selection?, target_language, partial}}
active: {name, selection?, captures: number}
capabilities: ["profile_import"]
```

模型名稱與 ID 不是擴充功能的白名單；名稱、語言與模型來源均可擴充。狀態與 selection.kind 則屬有限協定。缺少必要版本／能力時顯示升級或不可用提示，不靜默降級至另一模型。

## 設定保存與服務相容

- 共用儲存層為 `backend.config_store`。
- `load_snapshot(path)` 回傳 `(config, revision)`。
- `save_config(path, config, expected_revision=...)` 回傳新 revision；過期快照以 `RevisionConflictError` 拒絕，GUI 保留使用者尚未保存的輸入並提示重新載入。
- 新服務清單為 `translation.service_profiles`；既有 `translation.vllm.profiles` 保留 adapter，避免直接刪除舊設定。
- 明確的 `service_profiles: []` 是有效的空清單；不能再由 legacy adapter 補回已刪除的服務。`translation.default_selection` 保存 local／profile 選擇或明確的 None，舊 `default_profile_id` 僅作相容。
- 原子替換的設定文件內嵌 `_stt_tts_store` revision／digest；缺少舊 `.revision` sidecar 不會失去衝突保護。既有無版本文件或外部編輯以 52-bit digest 產生版本 token，保存時限制在 JavaScript safe integer 範圍。revision 用於相等性比較，不能假設外部編輯後仍單調遞增。
- profile 的 engine、model、API 類型、URL 補全與引擎專有 options 必須保存原意；Ollama、NLLB 與相容 API 不因搬移儲存位置而換成不同引擎。
- GUI 保存與瀏覽器遷移採同一個原子保存及衝突處理機制。

## 舊資料遷移

請求包含 `type: "desktop_import_profiles"`、`version: 2`、`profiles` 與選用的 `expected_revision`。遷移僅透過本機受信任來源的管理入口處理；一般網頁不能任意改寫後端設定。

結果以 `desktop_import_result` 回覆 `ok`、`mapping`、`revision` 和個別 `errors`。`mapping` 將舊瀏覽器 profile ID 對應至新目錄 ID；失敗項目不當成已完成。

- 匯入必須可重試、可去重；同名不同內容不可自動覆蓋。
- 憑證由擴充功能原有受限儲存讀出，只傳送給使用者已設定的本機後端；一般目錄、畫面快照與日誌不包含憑證。
- 只有收到後端保存確認，才將原選中 ID 轉換為新目錄 ID。
- 遷移來源與確認結果可供復原；成功項目不因使用者稍後在 GUI 刪除而自動復活。
- 舊後端或離線時保留原資料並標示遷移未完成。
- 部分成功時，即使 `ok: false`，已確認的 mapping 仍累積保存；重試只送尚未完成項目。完成紀錄以持久 `server_id` 分隔，不能因後端 `instance_id` 改變而重新匯入已刪除資料。

## 狀態與生效時機

| 類型 | 保存／生效方式 | 對既有擷取的影響 |
|---|---|---|
| 模型目錄與 profile 保存 | 下次目錄刷新可見；新擷取解析最新設定 | 不修改正在執行的設定快照 |
| 目標語言／翻譯行為 | 明確沿用桌面預設或分頁覆寫；下次擷取生效 | 其他分頁不變 |
| STT 預設與模型路徑 | GUI 明確提示目前需要重啟或下次擷取生效 | 不自動重啟 |
| Host／Port | 保存後由下一次服務啟動使用 | 正在監聽的位址不因輸入框修改而變動 |
| 字幕外觀與佈景 | 介面偏好可立即套用 | 不切換模型或翻譯服務 |

GUI 的 Starting／Ready／Stopping／Stopped／Error 與擷取階段分開；Ready 只代表管理服務已就緒。健康端點 `/health` 至少回傳 `service: "stt-tts"`、`instance_id`、`ready: true`。GUI runner 產生 `STT_INSTANCE_ID` 給子程序，必須確認回應相同才顯示 Ready。

Popup 的目錄刷新與擷取狀態獨立更新。清單逾時不阻擋 Stop。下次選擇不取代目前設定快照；多分頁共享翻譯服務但可使用不同目標語言。

v2 擷取由後端解析最新預設，並於 loading／capturing 狀態附上實際 `snapshot.stt` 與 `snapshot.translation`。前端以這個回應更新目前設定；不能只顯示送出的預期值。WhisperLiveKit 的 singleton 由受鎖保護的工廠處理，讓新擷取的模型變更確實建立相應引擎，既有擷取保留原本物件。

## 視覺規則

沿用使用者附圖的藍色主色、側欄、卡片、細邊框與明確留白。以 Segoe UI 和系統中文字型保持 Windows 可讀性；以一致尺寸的線條圖示輔助導覽。

- GUI 與擴充功能都提供 Light／Dark／System；偏好各自持久保存，離線仍可切換。
- 主要按鈕為藍色實心、次要按鈕為淡色或描邊、危險動作使用紅色；不把所有按鈕做成同等強度。
- 常用操作使用清楚標籤；協定、進階 runtime 與詳細錯誤放在次要資訊層。
- Target language 使用獨立輸入列，建議語言可選也可自由輸入，說明置於下方或提示區。
- 刷新是同步狀態列上的次要動作；不塞入模型下拉選單的邊框內。
- Popup 以 380×560 為最低實測基準，開始／停止固定可見；Options 承載偏好與遷移資訊。
- 所有佈景涵蓋輸入、原生選單、焦點、停用、警告、錯誤與捲動區；縮放與鍵盤可操作。

開放集合由目錄與 adapter 處理；佈景模式、協定版本、狀態名稱等有限集合使用集中定義。測試必須包含自訂模型、未列在建議中的語言與遷移衝突，避免僅對附圖範例有效。
