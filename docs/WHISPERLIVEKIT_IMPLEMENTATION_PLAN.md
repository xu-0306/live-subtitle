WhisperLiveKit Merge Plan

Goals
- Replace the current backend with a new WhisperLiveKit-based server.
- Keep the extension UI for translation selection.
- Default translation to local NLLB, with optional Ollama/OpenAI.
- Switch audio ingestion to webm/opus (MediaRecorder).

Phase 1: Architecture and Protocol
- Decide on embedding WhisperLiveKit in a custom FastAPI server (single process).
- Keep the existing subtitle JSON contract for the extension/overlay.
- Define the WebSocket payload mapping from WhisperLiveKit results to subtitle messages.
- Confirm audio format: webm/opus chunks from MediaRecorder.

Phase 2: New Backend (WhisperLiveKit)
- Create a fresh backend server using WhisperLiveKit's TranscriptionEngine + AudioProcessor.
- Add a translation adapter that supports:
  - NLLB (default, local, via transformers)
  - Ollama (local LLM)
  - OpenAI (API)
- Remove DeepL support.

Phase 3: Extension Updates
- Update offscreen capture to MediaRecorder webm/opus.
- Update settings UI to select translation engine/model.
- Send translation config to backend on start.

Phase 4: Integration and Tuning
- Wire STT -> translation -> subtitle JSON with partial/final handling.
- Prevent status messages from masking subtitles.
- Add simple health/test endpoints or test messages.

Phase 5: Docs and Verification
- Update requirements and README to reflect WhisperLiveKit/FFmpeg.
- Add a minimal validation flow:
  - STT only
  - STT + NLLB
  - STT + Ollama/OpenAI
