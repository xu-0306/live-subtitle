# Local translation and API services

See [the current workflow, model research and routing contract](TRANSLATION_SERVICES.md)
for detailed usage and verified limitations.

1. Double-click `Start-GUI.bat`, or run `python -m gui`.
   See [launcher setup](GUI_STARTUP.md) for dependency checks.
2. Use **Local translation → Download model** to prepare GGUF weights and llama.cpp.
   **Add Hugging Face GGUF…** accepts a public repository and GGUF filename, including shards.
3. Use **Translation services** to save cloud or self-hosted API profiles. Existing
   `translation.vllm` connections are read as ordinary compatible APIs. The
   launch helper has been archived in `legacy/vllm-launch-helper/` and removed.
4. Reload the updated extension and click **Refresh desktop services**. Choose an
   installed model or API profile, enter a language name/code, and start capture.
5. Stop existing captures before changing providers. The last capture releases
   the managed model and its GPU memory. Saving/downloading does not start inference.

Language suggestions are not an allowlist. LLM language names/codes pass through
to the translation prompt. NLLB retains its tokenizer language-token contract and
reports unsupported targets rather than silently translating into Chinese.

## Runtime and storage

Managed runtime currently supports Windows x64 with NVIDIA CUDA 12.4 or CPU mode.
The desktop **Local translation → Advanced → GPU offload** control supports:

- `Auto` (`gpu_layers = -1`): llama.cpp chooses the highest fitting layer count at
  model load and keeps an STT-aware VRAM margin for Whisper and shared peaks.
- `0`: CPU translation with no model layers offloaded to the GPU.
- A positive value: an exact manual layer limit; load errors are reported instead
  of silently changing the requested value.

Auto sizing uses GGUF metadata inside llama.cpp rather than a table of known model
architectures. Memory estimates remain planning inputs and do not guarantee speed
or fit for every context/layer configuration.

Model metadata lives in `backend/local_models.json`; custom pinned HF metadata is
stored in user config under `local_llama.custom_models`. Files live under:

```text
%APPDATA%/Live Subtitle/
  config.yaml
  Local Translation Models/
    downloads/
    models/<model-id>/
    runtimes/<runtime-version>/<kind>/
    runtime.log
```

Each artifact must pass size and SHA256 verification; interrupted downloads retain
resumable partial files. Versioned runtime archives use checked staging extraction.
The runtime binds to loopback, uses a fresh in-memory API token, and belongs to its
backend process. Windows Job Objects terminate assigned children if the backend is
terminated, including the GUI's normal stop/restart path. Unrelated runtimes are not touched.

The translator has one inference slot, a 2048-token default context and bounded
response length. Qwen3/Qwen3.5 candidates disable thinking for subtitle latency.
There is no GPU compute reservation or guaranteed Whisper/translation latency.
The source build still requires Python, PySide6 and STT dependencies; this is not
a standalone installer.

## Validation

```powershell
python -m pytest tests backend/tests -q -p no:cacheprovider
node --test chrome-extension/tests/capture-settings.test.js chrome-extension/tests/connection-settings.test.js chrome-extension/tests/translation-routing.test.js
```

Opt-in real runtime smoke (downloads only with `--download`):

```powershell
python tests/smoke_managed_runtime.py --root .work/managed-smoke --model-id qwen35-2b-q4 --download
```

The smoke writes a token-free result and stops its child. Qwen3.5 2B has passed on
RTX 3080 10GB; that short translation check is not a simultaneous Whisper benchmark.
RTX 3090/8GB latency and broader translation quality remain unmeasured.
