# GUI／擴充功能改版驗收

日期：2026-09-13。基準提交：`1aee2a8`。本次由三個 `gpt-5.6-luna`／`max` 子代理分別實作後端、GUI、擴充功能；主代理整合、檢查實際畫面與執行驗收。

工作項目與尚待確認內容見 [追蹤計畫](../../UI_SYNC_REDESIGN_PLAN.md)，協定見 [共用契約](../../UI_SYNC_CONTRACTS.md)，使用方式見 [翻譯服務文件](../../TRANSLATION_SERVICES.md)。

## 已實作範圍

- PySide6 GUI 改為側欄、卡片與共用元件樣式；Light／Dark／System 可保存偏好。
- Popup 與 Options 分工，擷取操作固定在底部；語言提示、服務刷新與模型欄位分開排列。
- 後端 v2 清單統一 STT／翻譯模型與 API profiles；擴充功能保留有來源識別的離線快取並自動更新。
- 舊瀏覽器 profiles 經確認回應遷移至後端；保留引擎、模型、API 類型、URL 補全、憑證與 ID 對應，部分失敗可重試。
- 共用設定以跨程序檔案鎖、版本檢查及原子替換保存，拒絕過期 GUI 快照。
- GUI 等待相符的後端健康檢查才顯示 Ready；擷取狀態另外回報模型載入與實際設定快照。
- 桌面預設與分頁覆寫分開，保存服務不直接載入模型；保留同模型多語言、不同翻譯服務互斥及最後使用者離開後回收。

## 本輪結果與截圖

- [Python 回歸輸出](artifacts/python-tests.txt)：**87 passed**（含最終空清單與 API 類型修正）。
- [Node 回歸輸出](artifacts/node-tests.txt)：**16 passed，0 skipped**，包含實際 Chromium 的固定操作列檢查；路由測試改為等待可觀察狀態，避免並行執行時固定延遲造成偶發失敗。
- [管理協定 13 項](artifacts/management-result.json)、[Qt／後端 14 項](artifacts/gui-backend-result.json)、[Chromium 7 項](artifacts/browser-result.json)、[真實音訊／GPU 11 項](artifacts/audio-result.json) 通過。
- Qt 的 Test connection 由真實按鈕經真實後端呼叫隔離的 HTTP 測試供應商；GPU 項目則使用真實模型。兩者不混為外部商用 API 驗收。
- GUI 六個頁面的雙色截圖均保存在 [screenshots](screenshots/)；900×640／150% 是 Qt 渲染尺度，沒有宣稱已切換 Windows 實體顯示設定。

| 畫面 | 淺色 | 深色 |
|---|---|---|
| GUI Server | [1100×760](screenshots/gui-light-0-server-1100x760.png) | [1100×760](screenshots/gui-dark-0-server-1100x760.png) |
| GUI 翻譯服務 | [1100×760](screenshots/gui-light-5-translation-services-1100x760.png) | [1100×760](screenshots/gui-dark-5-translation-services-1100x760.png) |
| Popup | [380×560](screenshots/popup-light.png) | [380×560](screenshots/popup-dark.png) |
| Popup 捲動 | [固定操作列](screenshots/popup-light-scrolled.png) | [固定操作列](screenshots/popup-dark-scrolled.png) |
| Options | [淺色](screenshots/options-light.png) | [深色](screenshots/options-dark.png) |

[離線保留選擇畫面](screenshots/popup-offline.png)。截圖為隔離測試資料；畫面中的埠號與測試服務名稱不是產品預設。

## 證據分類

| 驗證 | 方式 | 能證明的範圍 |
|---|---|---|
| Python 回歸 | `pytest tests backend/tests` | 設定版本衝突、profile 相容、狀態、Qt 元件與服務生命週期；包含 mock 與實際子程序測試 |
| Node 回歸 | `node --test chrome-extension/tests/*.test.js` | 清單快取、請求順序、遷移部分失敗／重試、實際 JS 路由；Chrome 音訊 API 為模擬 |
| 管理協定 | [verify_management.py](verify_management.py) | 真正啟動 FastAPI，驗證健康實例、自訂 STT 清單、遷移、過期保存、v1 相容與來源限制；不做推論 |
| Qt／後端整合 | [verify_gui_backend.py](verify_gui_backend.py) | 真正 Qt 控件編輯／保存後，執行中的後端反映服務名稱與預設；取消預設、刪除最後服務不復活 |
| 瀏覽器整合 | [verify_browser.cjs](verify_browser.cjs) | 真正 Chromium 載入未封裝擴充功能，驗證主題、Popup 尺寸／捲動、清單同步、IndexedDB 憑證遷移與離線保留選擇 |
| 音訊／GPU | [verify_audio_pipeline.py](verify_audio_pipeline.py) | 真正 WebM／Opus 送入後端，由 Whisper 與 GGUF 產生字幕；不冒充瀏覽器 tabCapture 驗證 |

所有驗證使用隔離設定與瀏覽器 profile；假 API 憑證只用於遷移測試。工作目錄、模型與快取位於忽略的 `.work/`，未修改使用者日常設定。

## 真實音訊結果與限制

使用 [OpenAI Whisper 的公開 JFK 測試音訊](https://github.com/openai/whisper/blob/main/tests/jfk.flac)，轉為 WebM／Opus 串流。實測主機為 **RTX 3080 10GB**，模型為 **Whisper Small + Qwen3.5 2B Q4_K_M**，目標語言 `zh-TW`。

實際產生了包含 `ask not what your country can` 的原文字幕與中文譯文；同一模型另以 `es-MX` 做短句翻譯，其他翻譯服務被正確拒絕，None 可共存。最後使用者停止後，確認 llama.cpp 程序在後端尚未關閉時已退出。

這是端到端音訊功能 smoke，沒有宣稱多語言翻譯品質、長時間延遲或 RTX 3090／8GB 裝置效能合格。前段不穩定字幕曾出現錯詞及簡體字，不能把「有譯文」當成品質驗收。

原生 Chrome 工具列的 tabCapture 授權仍需人工驗收：自動化 `action.openPopup()` 無法代替實際點擊授權；本環境 computer-use 將隔離 Chromium 視窗誤識別成另一個 Chrome 程序，無法操作該視窗。保留 [verify_live_capture.cjs](verify_live_capture.cjs) 供可操作原生工具列的環境重跑。真實瀏覽器渲染、JS 路由測試、直接 WebSocket 音訊驗證分別記錄，不能合併宣稱已完成真實分頁擷取。

## 重跑方式

```text
rtk proxy python -X utf8 -m pytest tests backend/tests -q -p no:cacheprovider --basetemp .work/pytest-ui-sync-redesign
rtk proxy node --test chrome-extension/tests/*.test.js
rtk proxy python -X utf8 docs/implementation/ui-sync-redesign/verify_management.py
rtk proxy git diff --check
```

[run_browser_verification.py](run_browser_verification.py) 啟動隔離後端，執行 `UI_SYNC_VERIFY_SCRIPT` 指定的驗證檔並收尾。預設執行 `verify_browser.cjs`，也可指定 `verify_gui_backend.py` 或 `verify_audio_pipeline.py`。

- 瀏覽器：設定 `UI_SYNC_BROWSER` 為測試 Chromium 路徑；`PLAYWRIGHT_MODULE` 指向已安裝的 Playwright 模組。預設查找 `.work/ui-validation/node_modules/playwright`，不依賴產品安裝。
- Node 的 `popup-layout` 是選用的實際瀏覽器回歸；沒有 Playwright 或瀏覽器時會明確 skipped。欲重現包含該項的全數通過結果，需提供上述模組與瀏覽器設定。
- GPU 音訊：設定 `UI_SYNC_MANAGED=1`；需要既有 `.work/managed-smoke` runtime／模型與專案根目錄 `small.pt`。驗證器不自行下載模型。
- 真實分頁：設定 `UI_SYNC_HEADED=1`、`UI_SYNC_MANUAL=1`、`UI_SYNC_VERIFY_SCRIPT=verify_live_capture.cjs`；在開啟的隔離瀏覽器中點選擴充功能並開始擷取。腳本會播放公開音訊、等待真實字幕並停止。
- `NUMBA_CACHE_DIR` 與 `TRITON_CACHE_DIR` 由驗證器指向可寫的專案快取，避免 sandbox 禁止寫入第三方套件／使用者目錄造成初始化卡住。

## 尚待人工確認

- 原生工具列授權、真實分頁開始／停止、頁籤關閉與多分頁切換。
- 真實 Windows 125%／150% 顯示設定切換與跨螢幕操作；已做 Qt 離屏尺寸／縮放渲染不等同實體顯示器驗收。
- 真實雲端供應商的 API key／帳單環境；本次遷移用假憑證，沒有拿使用者憑證呼叫外部服務。
- 長時間音訊、多語言品質及其他 GPU 容量，沿用既有專案待辦，不混入本次 UI 通過項目。

以上項目在追蹤計畫保留未勾選，未推送或建立提交。
