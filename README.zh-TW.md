# Live Subtitle

[English](README.md)

Live Subtitle 能將 Chrome／Chromium 分頁正在播放的音訊轉換成網頁即時字幕。語音辨識由本機 Whisper 後端負責；翻譯可以完全在本機執行，也可以使用使用者選擇的服務連線。

本項目由三個部分組成：

- Chrome 擴充功能：擷取分頁音訊並顯示字幕疊加層。
- Windows 桌面程式：控制本機後端、模型與設定。
- FastAPI 後端：執行串流 STT 並協調選用的翻譯功能。

> [!NOTE]
> Live Subtitle 目前是公開預覽版本。已打包的 Windows 程式與 Chrome 擴充功能可從 [Releases 頁面](https://github.com/xu-0306/live-subtitle/releases)下載。

## 主要功能

- 擷取目前瀏覽器分頁的 WebM／Opus 音訊。
- 透過 WebSocket 將音訊串流至本機後端。
- 使用 WhisperLiveKit 產生即時語音字幕。
- 直接在目前網頁中顯示字幕。
- 可調整字幕位置、大小、透明度、歷史行數及 partial 結果。
- 每次擷取都能選擇 STT 模型、來源語言、翻譯方式及目標語言。
- 只在真正需要時載入模型。
- 支援多個瀏覽器分頁，並協調共用的後端資源。

## 運作方式

```text
Chrome／Chromium 分頁音訊
            ↓
Manifest V3 擴充功能
            ↓ WebSocket
本機 FastAPI 後端
            ↓
WhisperLiveKit STT
            ↓
選用的翻譯
            ↓
網頁字幕疊加層
```

擴充功能負責下一次擷取所使用的選項；桌面程式則負責持久設定、模型檔案、翻譯服務 profiles 及後端生命週期。

## 翻譯選項

翻譯並非必要功能。選擇 **None** 時仍可正常產生語音字幕。

| 選項 | 負責內容 |
| --- | --- |
| **None** | 顯示語音辨識結果，不進行翻譯。 |
| **Local translation** | 下載並管理本機 llama.cpp runtime 與 GGUF 翻譯模型，在這台電腦上執行推論。 |
| **Translation services** | 保存本機、區網、自架或雲端服務的連線，例如 Ollama、OpenAI-compatible API、vLLM 與相容 adapter；由所選服務執行翻譯。 |

**Local translation** 與 **Translation services** 對使用者而言都提供翻譯能力，但執行方式不同：前者管理本機 runtime 與模型資產，後者管理已存在之翻譯服務的連線。

## 桌面程式

桌面 GUI 依照職責分頁：

| 頁面 | 用途 |
| --- | --- |
| **Server** | 啟動、停止、重新啟動及檢查本機後端。 |
| **Local translation** | 下載並管理本機 GGUF 模型與由程式管理的 llama.cpp runtime。 |
| **STT models** | 選擇並檢查 Whisper 語音辨識模型。 |
| **Tuning** | 調整字幕與串流行為。 |
| **Translation services** | 建立並測試雲端、區網或自架的翻譯服務 profiles。 |
| **Settings** | 管理程式行為、設定檔位置、主題及共用模型儲存根目錄。 |

後端與擴充功能會交換共用的模型與服務目錄，因此擴充功能顯示的擷取選項會反映桌面程式提供的可用資源。

## 瀏覽器擴充功能

Manifest V3 擴充功能負責：

- 只在開始擷取時請求分頁音訊權限。
- 在 offscreen extension document 中維持音訊擷取。
- 將音訊傳送至設定的本機 WebSocket endpoint。
- 在網頁字幕疊加層顯示 partial 與 final 字幕。
- 保存擷取偏好與翻譯選項，供之後的工作階段使用。

預設 endpoint 是 `ws://127.0.0.1:8765/asr`，後端預設只接受本機連線。

## 模型與應用程式資料

Release packages 不包含模型。STT 模型會在第一次需要時下載；受管理的 GGUF 翻譯模型則從 **Local translation** 頁面下載。

Windows 預設資料結構：

```text
%APPDATA%\Live Subtitle
├─ config.yaml
├─ Speech models\
└─ Local Translation Models\
```

只需在 **Settings → Model storage** 設定一次共用儲存根目錄。Whisper 權重與本機翻譯資產會分別保存在根目錄下的不同資料夾。

舊版 `%APPDATA%\STT-TTS\config.yaml` 可複製到新的應用程式目錄；舊目錄不會被刪除，設定中明確指定的模型路徑也能繼續使用。

## 設定模型

執行時使用以下優先順序：

1. 瀏覽器擴充功能為本次擷取選擇的項目
2. 桌面 GUI 保存的預設值
3. `backend/config.yaml` 中的內建預設值

擴充功能可選擇 STT 與翻譯行為，但不能覆蓋桌面程式的模型儲存路徑。公開目錄資料及傳送至 content script 的訊息不包含 API key、憑證或本機模型路徑。

## 本機優先行為

在預設流程中，瀏覽器音訊只會傳送到本機後端進行語音辨識。若選擇翻譯服務 profile，辨識後的文字會傳送至該服務；服務可能位於本機、區網或雲端。使用 Local translation 時，STT 與翻譯都在同一台電腦上處理。

停用翻譯時不會載入翻譯模型。受管理的本機翻譯 runtime 會按需啟動，並在最後一個使用者中斷連線後釋放。

## 取得應用程式

[Releases 頁面](https://github.com/xu-0306/live-subtitle/releases)提供：

- Windows x64 portable 桌面程式。
- 可透過開發人員模式手動載入的 Chrome／Chromium 擴充功能。

各版本的安裝步驟、檔案名稱及已知限制會附在對應的 Release 說明中。

## 從原始碼執行

環境需求：

- Windows 與 Python 3.10 或更新版本
- Chrome、Edge 或其他相容的 Chromium 瀏覽器
- 依 CUDA 或 CPU 環境安裝適合的 PyTorch
- 安裝 `backend/requirements.txt` 與 `gui/requirements.txt` 中的套件

建立環境：

```powershell
git clone https://github.com/xu-0306/live-subtitle.git
Set-Location live-subtitle
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install --upgrade pip
python -m pip install -r backend\requirements.txt
python -m pip install -r gui\requirements.txt
```

請另外安裝符合 CUDA 或 CPU 環境的 PyTorch，接著啟動桌面程式：

```powershell
.\Start-GUI.bat
```

只執行環境檢查：

```powershell
.\Start-GUI.bat --check
```

最後在 `chrome://extensions` 將 `chrome-extension` 目錄載入為未封裝擴充功能。

## 專案結構

- `backend/` — FastAPI 伺服器、串流 STT、翻譯、模型管理與設定
- `gui/` — PySide6 桌面程式
- `chrome-extension/` — 瀏覽器擷取、設定、service worker、offscreen 音訊與字幕疊加層
- `docs/` — 架構、模型、啟動及 UI 行為文件
- `scripts/` — 原始碼啟動輔助工具

## 相關文件

- [翻譯服務與模型選擇](docs/TRANSLATION_SERVICES.md)
- [受管理的本機翻譯](docs/LOCAL_TRANSLATION_MVP.md)
- [架構圖](docs/STT_TRANSLATION_ARCHITECTURE.md)
- [GUI 啟動與打包](docs/GUI_STARTUP.md)

## 目前狀態

Live Subtitle 仍在持續開發中。語音辨識與翻譯品質取決於所選模型、語言、來源音訊及可用硬體。GPU 並非必要，但 CPU 與 GPU 環境的本機推論速度可能有明顯差異。

Chrome 擴充功能目前需透過開發人員模式手動載入，Windows 執行檔也尚未進行程式碼簽署。

## Credits

- [WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit) — 串流 STT 引擎

## 問題回報

請[建立 GitHub Issue](https://github.com/xu-0306/live-subtitle/issues)，並附上 Windows 版本、使用的 STT 模型、翻譯方式、CPU／GPU 環境及相關錯誤或日誌。
