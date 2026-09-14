# Backend 設定與模型管理

## 目標
- 模型下載來源統一由 `backend/model_manager.py` 管理。
- 設定優先順序：**擴充功能連線設定 > GUI 使用者設定 > backend/config.yaml 預設**。
- GUI 僅保留非重複設定（host/port、模型快取目錄、字幕顯示、STT 穩定參數）。

## 設計摘要
- 伺服器啟動時載入 GUI 寫入的使用者設定檔（若無則使用預設）。
- 擴充功能連線後送出的 `config` payload 只覆蓋允許欄位（例如模型大小）。
- 若擴充功能指定的模型不存在，後端即自動下載至 `model_cache_dir`。

## 核心規則
- 擴充功能設定永遠覆蓋 GUI 設定（但僅限白名單欄位）。
- GUI 設定覆蓋預設 `config.yaml`。
- 模型下載與路徑解析一律走 `backend/model_manager.py`。
