# 即時字幕管線修正計畫

更新日期：2026-09-21

## 1. 目標

本計畫處理目前即時字幕的三類問題：

1. 同一段語音跨 `seq` 重複顯示、字幕錯位或舊翻譯覆蓋新原文。
2. STT、翻譯排隊及畫面更新的延遲來源無法分辨。
3. 小型翻譯模型偶爾出現語意生硬、目標語言文字系統不一致或不必要的來源語言殘留。

內容可能來自遊戲、旅遊、日常生活、訪談、教學或其他未知領域。因此翻譯提示詞必須保持領域中立；本計畫不以內容關鍵字猜測領域，也不為任何單一直播類型加入特例。

## 2. Anti-hardcode 分類與設計原則

### 2.1 問題分類

| 項目 | 分類 | 設計方式 |
|---|---|---|
| 人類語音內容與主題 | 開放世界 | 不建立遊戲、旅遊、生活等關鍵字清單；使用通用翻譯契約 |
| 使用者輸入的目標語言 | 開放集合 | 保留自由語言名稱或代碼；建議清單不是 allowlist |
| WebSocket 訊息類型、`seq`、request ID | 封閉協定 | 使用明確 schema、狀態機與完整測試 |
| 翻譯供應商的取消能力 | adapter boundary | 由 provider capability 描述，不在共用流程散布供應商名稱判斷 |
| 舊版 WhisperLiveKit 輸出差異 | 相容邊界 | 集中在 STT adapter，附版本註解與移除條件 |

### 2.2 必須維持的 invariant

- 同一個上游 ASR segment 的新快照只能修訂既有內容，不能被當成新語句重播。
- `seq` 的生命週期由字幕 chunk 決定，不受翻譯完成、失敗或逾時的時間控制。
- 翻譯 request 只能更新建立它的字幕 chunk，不能清除較新的 STT 狀態。
- 使用者選擇的目標語言是 prompt 的資料輸入，不是由內容領域推測。
- 模型可使用短期穩定前文判斷詞義，但只能輸出目前 chunk 的譯文，不能重譯或複製前文。
- 未列入建議清單的合法語言名稱或代碼仍須原樣傳到翻譯層。
- 真實重複說話必須保留；不得用模糊文字去重掩蓋狀態機錯誤。

## 3. 已確認的問題模型

### 3.1 累積 ASR 快照被誤認為新 final

WhisperLiveKit 可能依序提供同一行的累積快照：

```text
A -> A B -> A B C
```

目前後端只取 `response.lines` 的最後一項，並把非空內容視為 final。當四秒切段或翻譯完成使狀態被清空後，下一份累積快照可能取得新的 `seq`：

```text
seq 1: A B
seq 2: A B C
```

前端依照協定將不同 `seq` 顯示成兩列，因此重複的主要來源是後端的 segment／chunk 身分管理，而不是前端單純重複渲染。

參考：

- [WhisperLiveKit tokens alignment](https://github.com/QuentinFuxa/WhisperLiveKit/blob/main/whisperlivekit/tokens_alignment.py)
- [WhisperLiveKit audio processor](https://github.com/QuentinFuxa/WhisperLiveKit/blob/main/whisperlivekit/audio_processor.py)
- [SimulStreaming](https://github.com/ufal/SimulStreaming)

### 3.2 翻譯完成回呼控制了 STT 狀態

目前 mutable utterance、待翻譯文字、顯示 `seq` 及非同步 request 的責任互相交疊。舊 request 完成時可能推進 `seq` 或清除已包含較新語音的狀態，造成：

- 新原文配到舊翻譯；
- 較新的 utterance 被清除；
- 同一上游 segment 被配置新 `seq`；
- timeout、空輸出或 provider error 意外改變字幕邊界。

### 3.3 延遲尚未被分層量測

`segment_max_ms=4000` 只會在下一次 STT 結果抵達時被檢查，不是嚴格的四秒計時器。實際延遲可能同時包含音訊緩衝、ASR policy、GPU 資源競爭、翻譯排隊、模型推論、WebSocket 及渲染。

## 4. 目標架構

### 4.1 三層狀態分離

1. **ASR segment state**
   - 身分由 session generation 加上上游 segment timestamp／ID 組成。
   - 內容是可修訂的累積快照。
   - 同一 segment 的更新採 replace，不直接 append 整份文字。

2. **Subtitle translation chunk**
   - 建立時同步配置唯一 `seq`。
   - 保存不可變的 source text、目標語言及建立原因。
   - 四秒長句切割只建立新 chunk，不重設上游 segment 身分。

3. **Translation request**
   - 具有獨立 request ID 與 chunk revision。
   - 完成時只能更新對應的 chunk。
   - stale response 可以丟棄顯示結果，但不能修改當前 ASR state。

### 4.2 已提交前綴游標

長語句強制切段後，保存已提交的 token／timestamp 範圍：

```text
累積快照：A B
已提交 chunk：A B
下一快照：A B C
下一 chunk：C
```

優先以 timestamp 或上游 token 身分計算增量。只有在舊版 adapter 無結構資訊時才使用 longest suffix/prefix overlap，且必須限制在同一 segment 內。

### 4.3 Provider capability

把以下能力分開描述：

- 是否能取消本機等待 task；
- 是否能確認停止 provider 端計算；
- 最大安全併發；
- draft 是否可 latest-wins；
- final chunk 是否必須有序完成。

managed llama.cpp 維持單一 active inference。HTTP request 被取消不代表模型端一定停止生成，因此過時結果應靠 request/chunk 身分拒收，而不是依賴取消的副作用。

## 5. 領域中立的翻譯提示詞計畫

### 5.1 Prompt 契約

提示詞不包含遊戲、旅遊、生活或其他內容領域。共用模板只使用：

- 已知時的來源語言；
- 使用者本次擷取所選的目標語言名稱或代碼；
- 可選的短期穩定原文，只供消歧與術語判斷；
- 待翻譯的 STT 文字。

語意要求保持通用：

```text
Translate the transcript into {target_language}.
Use the standard writing conventions of the selected target language or locale.
Infer the intended meaning and terminology from the transcript and the optional
preceding context, without naming or explaining a content domain.
Preserve the speaker's meaning, tone, numbers, and proper nouns.
The transcript may be incomplete or contain recognition noise; do not invent missing facts.
When context is insufficient or ambiguous, use a conservative translation.
Translate only the current transcript. Return only its translation, without labels,
explanations, the source text, or a translation of the preceding context.
If the text is already in the target language, return it unchanged.
```

`{target_language}` 必須來自該 capture 已解析的設定快照，不可從字幕內容推測，也不可由模型種類覆寫。

### 5.2 短期 rolling context

模型不需要先輸出或內部路由到「遊戲／旅遊／生活」等領域。需要消歧時，由後端附帶最近的穩定原文，讓模型直接依上下文選擇詞義與術語：

```text
Preceding context for disambiguation only:
{stable_source_context}

Current transcript to translate:
{current_source_text}
```

rolling context 必須符合以下契約：

- 只使用已 final／已提交的來源原文，不使用未穩定 partial。
- 不使用先前模型譯文作為語意來源，避免錯譯逐段累積。
- 依集中式字元／token 預算及最大時間範圍限制，預設值須由實測決定，不散布魔術數字。
- capture 開始、重新連線、來源語言明顯改變或超過時間範圍時清空。
- 建立 request 時複製不可變的 context snapshot，之後的新字幕不能改變在途 request。
- context 僅供消歧；provider payload 必須明確區分 context 與目前待翻譯文字。
- 模型只能輸出目前 chunk 的譯文；若前文不足或互相矛盾，採保守翻譯，不補造領域資訊。
- 是否啟用及預算屬翻譯設定／provider capability，不依內容關鍵字切換。

對 Qwen3.5 2B，先以零 context 與小型 context 做 A/B，確認術語收益大於 prompt token、延遲及誤導風險後，才決定預設值。

### 5.3 語言值處理

- 現有語言標籤表只可作常見代碼的可讀描述，例如把 `zh-TW` 描述為 `Traditional Chinese (zh-TW)`。
- 未命中的語言值必須使用經驗證後的原始使用者值，例如 `Kiswahili`、`粵語（香港）`、`es-MX` 或 `isiZulu`。
- 不新增以 `if target == "zh-TW"` 為核心的 prompt 分支。
- 若未來需要 locale-specific guidance，必須是集中式、可選的 metadata；缺少 metadata 時仍走完整的通用契約，而不是失敗或偷偷改用其他語言。
- 不建立內容領域 classifier，也不以字幕中的單字切換 prompt。
- 專業術語表若未來需要加入，必須由使用者明確設定或由服務 profile 提供，不能由主題關鍵字自動猜測。

### 5.4 輸出驗證

首版只做不依賴特定領域的結構檢查：

- 空輸出；
- 額外標籤或說明；
- 完整複製來源文字的可疑輸出；
- 過時 request 的輸出。

語言文字系統或 source leakage 指標可作 telemetry，但不得用少量硬編碼字元表直接重寫模型輸出。自動 retry 預設關閉，避免小模型讓延遲成倍增加；是否重試需由量測結果決定。

## 6. 分階段實作

### Phase 0：可重播資料與可觀測性

- 保存匿名化的 `FrontData` 事件 fixture，以及一段可公開重播的 WebM／Opus 音訊。
- 記錄實際 WhisperLiveKit 版本、segment start/end、source revision、chunk ID、`seq`、request ID。
- 分別量測 audio receive、ASR processing/policy lag、translation queue、inference、WebSocket 與 render latency。
- 記錄 force-flush 原因、stale response 丟棄原因及每個 provider 的 active request 數。

### Phase 1：修正後端 segment 與 `seq` 狀態機

- 以 segment 身分更新 mutable snapshot。
- `seq` 在 immutable chunk 建立時產生。
- force-flush 後保存 committed cursor，只送出尚未提交的增量。
- 翻譯完成、逾時、取消、空輸出或 provider error 都不得清除較新的 ASR state。
- 保留舊版文字 overlap fallback，但集中在 STT adapter 並加上版本與適用條件。

### Phase 2：修正翻譯排程與 race

- draft revision 可 latest-wins。
- final chunk 不可遺失，依建立順序處理。
- managed local runtime 維持一個 active inference 及有限 pending 狀態。
- response 必須比對 session generation、chunk ID 及 request revision 後才能送到前端。
- 保持 `translation.partial=false` 時不翻譯不穩定 partial。

### Phase 3：補強前端協定

- 保留依 `seq` upsert，同一 `seq` 只更新同一列。
- 定義並測試 stale final、partial-to-final 與 out-of-order response 行為。
- 修正 CJK regex 的重複跳脫問題，但不加入模糊字幕去重。
- 增加協定違例 telemetry，協助發現後端送出衝突內容。

### Phase 4：套用領域中立、目標語言感知的 prompt

- 更新共用 prompt template，不加入任何內容領域敘述。
- 直接使用 capture 的目標語言快照。
- 建立有界、不可變的 stable-source context snapshot，僅供目前 chunk 消歧。
- 確保模型只回傳目前 chunk 的譯文，不複製或翻譯 context。
- 保持未知語言名稱／代碼可原樣傳遞。
- 同一模板覆蓋 managed llama、OpenAI-compatible 與其他聊天式 provider adapter；有特殊輸入 schema 的 NLLB／TranslateGemma 留在各自 adapter。

### Phase 5：模型 A/B

在管線 correctness 修復後，以完全相同的 transcript、prompt 與目標語言比較：

- Qwen3.5 2B Q4；
- 2B 較高精度版本；
- Qwen3.5 4B；
- 必要時的專用翻譯模型 adapter。

量測語意正確度、自然度、目標語言遵循、幻覺、p50/p95 推論延遲與 VRAM。模型升級不與狀態機修正綁在同一變更中。

### Phase 6：獨立驗證 WhisperLiveKit 升級

- 將目前 0.2.17 與釘定的新版本分開重播比較。
- 若新版本提供穩定的 diff/events 協定，優先在 adapter 使用結構化增量。
- 升級與狀態機修改分開提交及驗收。
- 執行 30 至 60 分鐘的跨內容 soak test。

## 7. 測試計畫

### 7.1 狀態與協定測試

| 情境 | 輸入 | 合格條件 |
|---|---|---|
| 累積快照 | `A -> A B -> force -> A B C` | 新 `seq` 只包含 `C` |
| ASR 回改 | `A B -> A X B` | replace，不變成 `A B A X B` |
| 真實重複 | 不同 segment 都說 `Thank you` | 兩句均保留 |
| 翻譯 race | 舊 request 在新 revision 前後完成 | 不清除或覆蓋新狀態 |
| 錯誤路徑 | timeout、空輸出、provider error | 不推進錯誤 utterance 狀態 |
| 排程 | draft burst 加多個 final chunk | draft latest-wins；final 完整且有序 |
| 前端 | raw 與 translation 使用同一 `seq` | 只有一列並正確更新 |
| Partial | partial 後接 final | 正確取代且不重複 |
| CJK layout | 中日文長句 | 使用正確 compact limit |

### 7.2 Anti-hardcode 測試

測試不能只重複最初觀察到的英文轉繁中案例，至少包含：

- 相同 prompt 路徑、不同內容領域：旅遊對話、生活閒聊、技術教學、訪談片段。
- 相同內容、不同目標語言：`zh-TW`、`ja`、`es-MX` 與未列入標籤表的 `Kiswahili`／`isiZulu`。
- 目標語言名稱使用 Unicode 自由輸入，例如 `粵語（香港）`，仍須原樣進入 provider payload。
- 字幕含 `game`、`travel` 等字樣時，不得改變 prompt 類型或路由。
- 同一個歧義詞搭配不同自然前文時，允許產生符合各自語境的譯法，但 provider payload 不包含預先分類的領域標籤。
- 無前文或前文不足時採保守譯法，不因測試句中的單一關鍵字臆測領域。
- context 從旅遊話題切換成技術話題時，目前 chunk 應優先於舊前文，且不輸出舊 context 的翻譯。
- 在途 request 使用建立當下的 context snapshot，不會被之後抵達的 segment 改寫。
- 真實重複說話與累積快照同時存在，證明修正不是依固定句子或模糊相似度通過。
- 使用不同 provider response shape 驗證 adapter，而不是為單一模型輸出寫 substring 判斷。

### 7.3 翻譯品質語料

固定評估集應包含多領域、長短句、不完整句、口語感嘆、專有名詞、code-switch 及 ASR 雜訊。評分採 adequacy、fluency、target-language adherence、proper-noun preservation 與 hallucination，不要求唯一逐字答案。

## 8. 驗收門檻

- 重播套件中的跨 `seq` 已提交前綴重複數為零。
- race 測試中，舊翻譯不會清除或覆蓋較新 STT state。
- 相同 `seq` 在擴充功能中永遠只有一列。
- managed llama active inference 不超過一個。
- final chunk 不遺失且順序穩定。
- 未列入建議清單的合法目標語言可完整傳到實際 provider payload。
- 不同內容領域走相同的通用 prompt 契約。
- rolling context 只能影響目前 chunk 的消歧，不得出現在輸出或污染下一個 request 的字幕身分。
- 延遲門檻依 Phase 0 的分層基準設定，不以目前單次觀察任意指定。

## 9. 建議變更切分

1. **後端 segment/chunk/seq 狀態機與重播測試**
2. **翻譯 request race、provider capability 與排程測試**
3. **前端 stale response、CJK regex 與協定測試**
4. **領域中立 prompt、開放語言測試與品質評估集**
5. **模型 A/B 報告**
6. **WhisperLiveKit 版本升級實驗**

上述變更分開提交，確保任何品質或延遲差異都能對應到單一原因。

## 10. 預計涉及檔案

- `backend/server.py`：ASR segment、chunk、`seq` 與 request 生命周期。
- `backend/translator.py`：領域中立 prompt、context/input 結構、語言描述 fallback、provider capability。
- `backend/translation_scheduler.py`：排程與 active/pending invariant。
- `chrome-extension/content/content.js`：顯示協定、stale response 與 CJK regex。
- `backend/tests/`、`tests/`、`chrome-extension/tests/`：重播、race、開放語言與前端回歸測試。

本文件只定義修正方案；開始實作前應先完成 Phase 0 fixture 與失敗測試，以免用觀察到的單一內容、語言或模型行為硬編碼出表面修復。
