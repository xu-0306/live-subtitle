# Backend EXE + GUI Packaging Spec (Draft)

## Goals
- Package the backend as a Windows `.exe` (x64) with a GUI for easy start/stop.
- Allow setting host/port and show the resulting WebSocket URL.
- Allow choosing a Whisper model (size or file path).
- Persist defaults and load them on next launch.
- If the selected model file is missing, download it and show progress.
- Provide translation settings in a separate view (tab/window).
- Support minimize-to-tray with quick controls.

## Non-goals (for this phase)
- Full-featured settings editor for every backend option.
- Auto-update mechanism for the exe.
- Bundling model files inside the exe.

## User Stories
- As a user, I can launch a GUI app and start the backend without a terminal.
- As a user, I can set `host`/`port` and see the WS URL I should paste into the extension.
- As a user, I can pick a model size or a local model file.
- As a user, I can save defaults and the app remembers them.
- As a user, if the model file is missing, the app downloads it and shows progress.

## Visual Direction (Inspiration)
- Card-based settings layout and grouping:
  - https://www.setproduct.com/blog/settings-ui-design
  - https://blog.logrocket.com/ux-design/designing-settings-screen-ui/
- Desktop app settings structure:
  - https://learn.microsoft.com/en-us/windows/apps/design/app-settings/guidelines-for-app-settings
- Component/desktop UI kits for layout cues:
  - https://www.figma.com/community/file/1140598727819890394/desktop-app-ui-design
  - https://www.figma.com/community/file/1341960932129965145/desktop-app-kit-by-figma
  - https://www.untitledui.com/components/settings-pages

## UI Layout (Proposed)
- **Top-level tabs**
  - Server
  - STT
  - Translation
  - Advanced (optional)
- **Server (tab)**
  - Host (text input, default `127.0.0.1`)
  - Port (number input, default `8765`)
  - WS URL (read-only, e.g. `ws://127.0.0.1:8765/asr`) + Copy button
  - Start / Stop / Restart buttons + status indicator
- **STT (tab)**
  - Model directory (text + Browse button)
  - Model dropdown populated from directory contents
  - Missing model prompt (ask before download)
  - Download progress bar + percent + bytes
- **Translation (tab)**
  - Engine: `nllb`, `ollama`, `openai`, `noop`
  - Target language (dropdown)
  - NLLB model (text)
  - Ollama host + model
  - OpenAI api key + model + base_url
  - Partial translation toggle
- **Defaults**
  - Save as default
  - Reset to defaults
- **Logs (optional)**
  - Read-only log view for backend status/errors

## Tray Support
- Minimize-to-tray with status icon
- Tray menu:
  - Start / Stop
  - Open UI
  - Quit

## Settings & Persistence
- Config file location (proposed):
  - `%APPDATA%/Live Subtitle/config.yaml` (Windows per-user)
  - If not present, copy defaults from bundled `backend/config.yaml`
- On Save:
  - Write `server.host`, `server.port`
  - Write `stt.model` as the selected model name
  - Write `stt.model_cache_dir` as the model directory
  - For custom (non-standard) names, set `stt.model_path` to the resolved file in the directory

## Model Selection Rules
1. Read `.pt` files from the selected model directory and populate the model dropdown.
2. Resolve the selected model to `${model_cache_dir}/${model}.pt`.
3. If file is missing and the model is standard (tiny/base/small/medium/large-v3):
   - Prompt the user to download
   - If accepted, start download and show progress
   - Disable Start until download completes
4. If file is missing and model is custom:
   - Show error and block Start

## Model Download
- Source: official Whisper model URLs (OpenAI) for `.pt` files (default)
- Validate checksum when available (optional but recommended)
- Progress:
  - Show total bytes (if Content-Length available)
  - Show percent + speed (optional)
- Cancel:
  - Optional in v1 (no cancel button unless requested)

## Backend Execution
- Start backend **in-process** using `uvicorn.Server` on a background thread:
  - Set `STT_CONFIG_PATH` env to point at the GUI config
  - Run `backend.server:app` with host/port from config
  - Keep Start/Stop/Restart in the GUI
- Stop backend gracefully:
  - Signal `server.should_exit` and wait for thread to exit
- When host/port changes:
  - Require restart (Stop + Start)

## Packaging
- Use PyInstaller to build `live-subtitle.exe` (Windows x64)
- Include:
  - `backend/` package
  - Default `backend/config.yaml`
  - GUI entrypoint (new)
- Output: `dist/live-subtitle/live-subtitle.exe`

## Risks / Notes
- Multiple concurrent sessions = multiple models = high GPU/CPU usage.
- Model download requires network access; handle offline errors clearly.

## Confirmed Decisions
- Target: Windows x64
- Include translation settings in a dedicated tab
- Support minimize-to-tray
- Use default OpenAI Whisper model URLs for downloads
- GUI toolkit: PySide6
- Auto-start backend on launch when config is valid

## Open Questions (Need Your Confirmation)
1. None
