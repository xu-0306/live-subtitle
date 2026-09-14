# Live Subtitle

[繁體中文](README.zh-TW.md)

Live Subtitle turns audio playing in a Chrome or Chromium tab into live, on-page subtitles. Speech recognition runs through a local Whisper backend, while translation can remain entirely on the device or use a service selected by the user.

The project combines three parts:

- A Chrome extension that captures tab audio and renders the subtitle overlay.
- A Windows desktop application that controls the local backend, models, and settings.
- A FastAPI backend that performs streaming STT and coordinates optional translation.

> [!NOTE]
> Live Subtitle is currently a public preview. Packaged Windows and Chrome extension downloads are available on the [Releases page](https://github.com/xu-0306/live-subtitle/releases).

## What it does

- Captures WebM/Opus audio from the active browser tab.
- Streams audio to the local backend over WebSocket.
- Produces live speech subtitles with WhisperLiveKit.
- Displays subtitles directly on the current page.
- Supports configurable subtitle position, size, opacity, history, and partial results.
- Lets each capture choose its STT model, source language, translation option, and target language.
- Manages local models without loading them until they are actually needed.
- Supports multiple browser tabs while coordinating shared backend resources.

## How it works

```text
Chrome/Chromium tab audio
          ↓
Manifest V3 extension
          ↓ WebSocket
Local FastAPI backend
          ↓
WhisperLiveKit STT
          ↓
Optional translation
          ↓
On-page subtitle overlay
```

The extension controls the choices for the next capture. The desktop application owns persistent settings, model files, translation-service profiles, and the backend lifecycle.

## Translation choices

Translation is optional. Speech subtitles continue to work when translation is set to **None**.

| Choice | Responsibility |
| --- | --- |
| **None** | Show the recognized speech without translation. |
| **Local translation** | Download and manage a local llama.cpp runtime and GGUF translation models. Inference runs on this computer. |
| **Translation services** | Save connections to local, LAN, self-hosted, or cloud services such as Ollama, OpenAI-compatible APIs, vLLM, and compatible adapters. The selected service performs translation. |

**Local translation** and **Translation services** provide the same user-facing capability through different execution paths: one manages on-device runtime assets, while the other manages connections to an already available service.

## Desktop application

The desktop GUI is organized by responsibility:

| Page | Purpose |
| --- | --- |
| **Server** | Start, stop, restart, and inspect the local backend. |
| **Local translation** | Download and manage on-device GGUF models and an app-managed llama.cpp runtime. |
| **STT models** | Select and inspect Whisper speech-recognition models. |
| **Tuning** | Adjust subtitle and streaming behavior. |
| **Translation services** | Create and test cloud, LAN, or self-hosted translation profiles. |
| **Settings** | Manage application behavior, settings location, theme, and the shared model-storage root. |

The backend and extension exchange a shared model and service catalogue, so capture choices reflect what the desktop application has made available.

## Browser extension

The Manifest V3 extension:

- Requests tab-capture permission only when capture starts.
- Keeps audio capture in an offscreen extension document.
- Sends audio to the configured local WebSocket endpoint.
- Displays partial and final subtitles in the page overlay.
- Stores capture preferences and translation selections for later sessions.

The default endpoint is `ws://127.0.0.1:8765/asr`. The backend accepts local connections by default.

## Models and application data

Models are not bundled with the Release packages. STT models are downloaded when first needed, while managed GGUF translation models are downloaded from the **Local translation** page.

The default Windows data layout is:

```text
%APPDATA%\Live Subtitle
├─ config.yaml
├─ Speech models\
└─ Local Translation Models\
```

Set the shared storage root once under **Settings → Model storage**. Whisper weights and local-translation assets remain separated beneath that root.

Older `%APPDATA%\STT-TTS\config.yaml` settings can be copied to the new application directory. The legacy directory is not deleted, and explicitly configured model paths remain supported.

## Configuration model

Runtime choices use the following priority:

1. Browser-extension selection for the current capture
2. Defaults saved by the desktop GUI
3. Defaults in `backend/config.yaml`

The extension may select STT and translation behavior, but it cannot override desktop model-storage paths. Public catalogue data and messages sent to content scripts omit API keys, credentials, and local model paths.

## Local-first behavior

In the default workflow, browser audio is sent only to the local backend for speech recognition. If a translation-service profile is selected, the recognized text is sent to that configured service, which may run locally, on a LAN, or in the cloud. Local translation keeps both STT and translation processing on the same computer.

No translation model is loaded when translation is disabled. A managed local translation runtime starts on demand and is released after its final user disconnects.

## Getting the application

The [Releases page](https://github.com/xu-0306/live-subtitle/releases) provides:

- A Windows x64 portable application.
- A Chrome/Chromium extension package for manual loading through Developer mode.

Release-specific installation steps, filenames, and known limitations are included with each Release.

## Run from source

Requirements:

- Windows with Python 3.10 or newer
- Chrome, Edge, or another compatible Chromium browser
- PyTorch installed for the intended CUDA or CPU environment
- Packages from `backend/requirements.txt` and `gui/requirements.txt`

Create the environment:

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

Install the appropriate PyTorch build separately, then start the desktop application:

```powershell
.\Start-GUI.bat
```

Run only the environment check with:

```powershell
.\Start-GUI.bat --check
```

Load the `chrome-extension` directory as an unpacked extension from `chrome://extensions`.

## Project structure

- `backend/` — FastAPI server, streaming STT, translation, model management, and configuration
- `gui/` — PySide6 desktop application
- `chrome-extension/` — browser capture, settings, service worker, offscreen audio, and subtitle overlay
- `docs/` — architecture, model, startup, and UI behavior documentation
- `scripts/` — source startup helpers

## Documentation

- [Translation services and model selection](docs/TRANSLATION_SERVICES.md)
- [Managed local translation](docs/LOCAL_TRANSLATION_MVP.md)
- [Architecture diagram](docs/STT_TRANSLATION_ARCHITECTURE.md)
- [GUI startup and packaging](docs/GUI_STARTUP.md)

## Current status

Live Subtitle is under active development. Recognition and translation quality depend on the selected models, language, source audio, and available hardware. A GPU is optional, but local inference speed can differ significantly between CPU and GPU configurations.

The Chrome extension currently requires manual loading through Developer mode, and the Windows executable is not yet code-signed.

## Credits

- [WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit) — streaming STT engine

## Feedback

Please [open a GitHub Issue](https://github.com/xu-0306/live-subtitle/issues) with your Windows version, selected STT model, translation choice, CPU/GPU environment, and relevant errors or logs.
