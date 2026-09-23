# Retired vLLM launch helper

Archived on 2026-09-23 at the user's request. This feature only generated
commands and duplicated the service editor; it did not install, download,
start or stop a vLLM runtime.

The original files are preserved as `.py.txt` snapshots, so they cannot be
imported by the application or collected by pytest. `default-profile.yaml.txt`
contains the removed example configuration, not a user's saved credentials.
This directory is not included in the desktop or extension release.

Removed from the active application:

- The helper widget, duplicate profile editor and nested scroll area.
- Command generation, launch parameters, styling and load/save hooks.
- The dedicated service-type choice and default helper profile.

Previously saved connections are read as ordinary OpenAI-compatible APIs.
Stable IDs, endpoints, HTTP model names, authentication and request settings
are retained. Launch-only runtime settings are discarded from normalized
profiles. A desktop save removes the old duplicate configuration section.
The backend's old HTTP configuration reader remains for existing headless
configurations; it has no launch, installation or download capability.

Use **Local translation** for managed llama.cpp/GGUF models, or
**Translation services → OpenAI-compatible API** for an already running server.
Historical implementation evidence may still name these archived files.
