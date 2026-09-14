"""Shared, versioned configuration storage and service-profile adapters.

The desktop application and the browser extension both refer to the same
configuration file.  This module keeps the file format deliberately boring
(YAML for the existing config, JSON when a caller explicitly uses ``.json``),
but adds an optimistic revision sidecar so a stale GUI snapshot cannot erase a
profile imported by the extension.

``translation.service_profiles`` is the normalized profile format.  Legacy
``translation.vllm.profiles`` entries remain readable and writable through an
adapter; they are never silently reinterpreted as Ollama or NLLB settings.
Secrets are accepted by the storage layer because an import may migrate an
authenticated profile, but callers must use :func:`public_profile` when
building a catalog.
"""

from __future__ import annotations

import copy
import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

import yaml


class RevisionConflictError(RuntimeError):
    """Raised when a write was based on an older configuration revision."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Configuration changed while editing (expected revision {expected}, "
            f"current revision {actual}). Reload and retry."
        )


# A second name is useful to callers that describe this as an optimistic
# concurrency conflict rather than a revision conflict.
ConfigConflictError = RevisionConflictError


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}
_SUPPORTED_ENGINES = {
    "managed_llama",
    "nllb",
    "ollama",
    "openai",
    "openai_compatible",
    "vllm",
}
_STORE_META_KEY = "_stt_tts_store"
# Revisions cross the WebSocket boundary and are commonly represented as a
# JavaScript Number.  Keep generated/content-derived tokens below the 53-bit
# integer precision limit (we use 52 bits for an extra margin).
_MAX_SAFE_REVISION = (1 << 53) - 1
_MAX_GENERATED_REVISION = (1 << 52) - 1


def _path_value(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.absolute()).casefold()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _revision_path(path: Path) -> Path:
    # Keep revision metadata out of the user-visible YAML document.  A suffix
    # rather than a fixed directory also works for isolated test/config roots.
    return path.with_name(path.name + ".revision")


@contextlib.contextmanager
def _process_lock(path: Path):
    """Serialize config writes across GUI/backend processes.

    ``threading.RLock`` protects callers in one interpreter only.  The small
    adjacent lock file uses the platform's advisory file-lock primitive so a
    GUI and a separately started backend cannot both pass the same revision
    check.  It is intentionally not a PID lock: process death releases it.
    """

    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            # LK_LOCK waits for a conflicting process and the OS releases the
            # byte automatically if that process exits unexpectedly.
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix.casefold() == ".json":
            data = json.load(handle)
        else:
            data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Configuration root must be a mapping: {path}")
    # The revision marker is committed in the same atomic document as the
    # config.  Strip it before returning so runtime callers see the historic
    # config shape and never accidentally pass storage metadata to a provider.
    data.pop(_STORE_META_KEY, None)
    return data


def _read_raw_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix.casefold() == ".json":
            data = json.load(handle)
        else:
            data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Configuration root must be a mapping: {path}")
    return data


def _document_digest(data: Mapping[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_revision(
    path: Path,
    document: Mapping[str, Any] | None = None,
    embedded: Mapping[str, Any] | None = None,
) -> int:
    revision_path = _revision_path(path)
    def digest_revision(value: Mapping[str, Any]) -> int:
        # A content-derived token gives an unversioned/external edit a stable
        # identity that cannot be confused with the initial revision 0.
        return max(1, int(_document_digest(value)[:13], 16))

    if isinstance(embedded, Mapping):
        try:
            revision = int(embedded.get("revision"))
            recorded_digest = str(embedded.get("digest") or "")
            if revision >= 0 and document is not None and recorded_digest == _document_digest(document):
                return revision
            if document is not None:
                return digest_revision(document)
        except (TypeError, ValueError):
            if document is not None:
                return digest_revision(document)
    # A missing sidecar is expected for old files and after a sidecar write is
    # interrupted.  The embedded marker is authoritative because it was
    # committed in the same atomic replacement as the document.  Only a truly
    # absent document starts at revision zero; an existing unversioned file is
    # identified by its content digest so an initial stale snapshot cannot win.
    if not path.exists():
        return 0
    try:
        with revision_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, dict):
            recorded_digest = value.get("digest")
            if recorded_digest and document is not None and recorded_digest != _document_digest(document):
                return digest_revision(document)
            value = value.get("revision")
        revision = int(value)
        return revision if revision >= 0 else 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        # A missing/corrupt sidecar must not prevent a user from opening the
        # app.  An existing document gets an opaque digest revision instead of
        # falling back to 0, preventing stale initial snapshots from winning.
        return digest_revision(document) if document is not None else 0


def _next_revision(current: int) -> int:
    """Increment a revision without producing a Number-unsafe token."""

    current = int(current)
    if current < 0 or current >= _MAX_SAFE_REVISION:
        raise OverflowError("Configuration revision exceeds JavaScript safe integer range")
    return current + 1


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _dump_document(path: Path, data: Mapping[str, Any]) -> str:
    if path.suffix.casefold() == ".json":
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    return yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True)


def _write_revision(path: Path, revision: int, document: Mapping[str, Any] | None = None) -> None:
    metadata: dict[str, Any] = {"revision": revision}
    if document is not None:
        metadata["digest"] = _document_digest(document)
    _atomic_write(_revision_path(path), json.dumps(metadata) + "\n")


def load_snapshot(path: str | os.PathLike[str]) -> tuple[dict[str, Any], int]:
    """Load a config and its optimistic-concurrency revision.

    Reading is side-effect free.  Config files created before this store
    existed start at revision ``0`` and receive a sidecar on their first save.
    """

    config_path = _path_value(path)
    with _lock_for(config_path):
        with _process_lock(config_path):
            raw = _read_raw_document(config_path)
            embedded = raw.get(_STORE_META_KEY) if isinstance(raw.get(_STORE_META_KEY), Mapping) else None
            document = copy.deepcopy(raw)
            document.pop(_STORE_META_KEY, None)
            return document, _read_revision(config_path, document, embedded)


def save_config(
    path: str | os.PathLike[str],
    data: Mapping[str, Any],
    expected_revision: int | None = None,
) -> int:
    """Atomically save ``data`` and return the new integer revision.

    ``expected_revision=None`` means an unconditional write.  All other
    callers participate in optimistic concurrency and receive
    :class:`RevisionConflictError` on stale snapshots.
    """

    if not isinstance(data, Mapping):
        raise TypeError("Configuration must be a mapping")
    config_path = _path_value(path)
    with _lock_for(config_path):
        with _process_lock(config_path):
            raw_current = _read_raw_document(config_path)
            embedded = raw_current.get(_STORE_META_KEY) if isinstance(raw_current.get(_STORE_META_KEY), Mapping) else None
            current_document = copy.deepcopy(raw_current)
            current_document.pop(_STORE_META_KEY, None)
            current_revision = _read_revision(config_path, current_document, embedded)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise RevisionConflictError(int(expected_revision), current_revision)
            new_revision = _next_revision(current_revision)
            document = copy.deepcopy(dict(data))
            document[_STORE_META_KEY] = {
                "revision": new_revision,
                "digest": _document_digest(document),
            }
            payload = _dump_document(config_path, document)
            _atomic_write(config_path, payload)
            _write_revision(config_path, new_revision, data)
            return new_revision


def _copy_mapping(value: Any) -> dict[str, Any]:
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _as_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _engine_from_profile(profile: Mapping[str, Any]) -> str:
    raw = _as_text(
        profile.get("engine")
        or profile.get("provider")
        or profile.get("type")
    ).casefold().replace("-", "_")
    connection = profile.get("connection")
    if not raw and isinstance(connection, Mapping):
        # Legacy extension entries identify OpenAI-compatible services through
        # their connection object rather than an engine-specific section.
        raw = "openai"
    # Legacy extension values and GUI labels are adapter inputs, not the
    # canonical storage vocabulary.
    aliases = {
        "openai-compatible": "openai_compatible",
        "openai_compat": "openai_compatible",
        "chatgpt": "openai",
        "local_llama": "managed_llama",
        "managed-llama": "managed_llama",
        "local": "managed_llama",
    }
    raw = aliases.get(raw, raw)
    if not raw:
        if isinstance(profile.get("nllb"), Mapping):
            raw = "nllb"
        elif isinstance(profile.get("ollama"), Mapping) or profile.get("host"):
            raw = "ollama"
        elif isinstance(profile.get("openai"), Mapping) or profile.get("api_key"):
            raw = "openai"
    if raw not in _SUPPORTED_ENGINES:
        raise ValueError("Unsupported translation service engine")
    return raw


def _profile_id(value: Any, fallback: str) -> str:
    profile_id = _as_text(value) or fallback
    if not _ID_RE.fullmatch(profile_id):
        raise ValueError("Profile id must contain only letters, numbers, '.', '_', ':' or '-'")
    return profile_id


def _endpoint_from_profile(profile: Mapping[str, Any], engine: str) -> str:
    if engine == "ollama":
        nested = profile.get("ollama")
        if isinstance(nested, Mapping):
            return _as_text(
                nested.get("host") or nested.get("base_url") or nested.get("endpoint")
            )
        return _as_text(profile.get("host") or profile.get("base_url") or profile.get("endpoint"))
    nested = profile.get(engine)
    if isinstance(nested, Mapping):
        return _as_text(
            nested.get("base_url") or nested.get("endpoint") or nested.get("url")
        )
    connection = profile.get("connection")
    connection_value = ""
    if isinstance(connection, Mapping):
        connection_value = _as_text(
            connection.get("api_url")
            or connection.get("apiUrl")
            or connection.get("base_url")
            or connection.get("baseUrl")
        )
    return _as_text(
        profile.get("base_url")
        or profile.get("baseUrl")
        or profile.get("endpoint")
        or profile.get("api_url")
        or profile.get("apiUrl")
        or profile.get("url")
        or connection_value
    )


def normalize_service_profile(
    profile: Mapping[str, Any],
    index: int = 0,
    *,
    source: str | None = None,
) -> dict[str, Any]:
    """Normalize one GUI/extension/legacy service entry.

    The function intentionally keeps unknown provider options under
    ``options``.  This lets a newer GUI round-trip fields from an older
    backend, while engine and identity remain validated at this boundary.
    """

    if not isinstance(profile, Mapping):
        raise ValueError("Service profile must be an object")
    engine = _engine_from_profile(profile)
    normalized = _copy_mapping(profile)
    fallback = f"profile-{index + 1}"
    normalized["id"] = _profile_id(profile.get("id"), fallback)
    normalized["name"] = _as_text(profile.get("name")) or f"Translation service {index + 1}"
    normalized["engine"] = engine
    normalized["mode"] = _as_text(profile.get("mode")) or (
        "local" if engine in {"managed_llama", "nllb"} else "external"
    )
    normalized["model"] = _as_text(
        profile.get("model")
        or profile.get("served_model")
        or (profile.get(engine, {}).get("model") if isinstance(profile.get(engine), Mapping) else "")
    )
    endpoint = _endpoint_from_profile(profile, engine)
    normalized["base_url"] = endpoint
    normalized["endpoint"] = endpoint
    connection = profile.get("connection")
    connection_map = connection if isinstance(connection, Mapping) else {}
    normalized["api_type"] = _as_text(
        profile.get("api_type")
        or profile.get("api_mode")
        or connection_map.get("api_type")
        or connection_map.get("apiMode")
        or "chat_completions"
    ) or "chat_completions"
    normalized["auto_complete_endpoint"] = bool(
        profile.get(
            "auto_complete_endpoint",
            profile.get(
                "auto_complete_api_url",
                profile.get(
                    "autoCompleteApiUrl",
                    connection_map.get(
                        "auto_complete_api_url",
                        connection_map.get("autoCompleteApiUrl", True),
                    ),
                ),
            ),
        )
    )
    try:
        max_concurrency = int(profile.get("max_concurrency", 0) or 0)
    except (TypeError, ValueError):
        max_concurrency = 0
    normalized["max_concurrency"] = max(0, max_concurrency)
    normalized["api_key"] = _as_text(
        profile.get("api_key")
        or profile.get("apiKey")
        or connection_map.get("api_key")
        or connection_map.get("apiKey")
    )
    options = _copy_mapping(profile.get("options"))
    # Preserve provider-specific fields without duplicating the secret in the
    # generic options bag.
    for key in ("runtime", "nllb", "ollama", "openai", "openai_compatible", "vllm"):
        if isinstance(profile.get(key), Mapping):
            options.setdefault(key, copy.deepcopy(dict(profile[key])))
    normalized["options"] = options
    if source:
        normalized.setdefault("source", source)
    return normalized


def _legacy_vllm_profiles(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    translation = config.get("translation")
    if not isinstance(translation, Mapping):
        return []
    vllm = translation.get("vllm")
    if not isinstance(vllm, Mapping):
        return []
    raw = vllm.get("profiles")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    profiles: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        try:
            # Explicitly preserve the vLLM adapter identity and fields.
            profile = normalize_service_profile({**dict(item), "engine": "vllm"}, index, source="legacy-vllm")
        except (TypeError, ValueError):
            continue
        profiles.append(profile)
    return profiles


def service_profiles(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return canonical profiles while preserving legacy vLLM entries."""

    translation = config.get("translation")
    if not isinstance(translation, Mapping):
        translation = {}
    raw = translation.get("service_profiles")
    has_explicit_catalog = isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
    if raw is None:
        raw = translation.get("profiles")
        has_explicit_catalog = isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                continue
            try:
                profile = normalize_service_profile(item, index, source=str(item.get("source") or "desktop"))
            except (TypeError, ValueError):
                continue
            if profile["id"] in seen:
                continue
            seen.add(profile["id"])
            profiles.append(profile)
    if not has_explicit_catalog:
        for profile in _legacy_vllm_profiles(config):
            if profile["id"] not in seen:
                seen.add(profile["id"])
                profiles.append(profile)
    return profiles


def service_profile_issues(config: Mapping[str, Any]) -> list[dict[str, str]]:
    """Describe malformed profile rows without exposing their contents."""

    translation = config.get("translation")
    if not isinstance(translation, Mapping):
        return []
    raw = translation.get("service_profiles")
    source = "service_profiles"
    has_explicit_catalog = isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
    if raw is None:
        raw = translation.get("profiles")
        source = "profiles"
        has_explicit_catalog = isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
    candidates: list[tuple[int, Any, str]] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        candidates.extend((index, item, source) for index, item in enumerate(raw))
    if not has_explicit_catalog:
        vllm = translation.get("vllm")
        legacy = vllm.get("profiles") if isinstance(vllm, Mapping) else None
        if isinstance(legacy, Sequence) and not isinstance(legacy, (str, bytes)):
            candidates.extend((index, item, "legacy-vllm") for index, item in enumerate(legacy))

    issues: list[dict[str, str]] = []
    used_ids: set[str] = {
        _as_text(profile.get("id"))
        for profile in service_profiles(config)
        if isinstance(profile, Mapping)
    }
    for index, item, item_source in candidates:
        if not isinstance(item, Mapping):
            profile_id = f"profile-{index + 1}"
            base_id = profile_id
            suffix = 2
            while profile_id in used_ids:
                profile_id = f"{base_id}-{suffix}"
                suffix += 1
            used_ids.add(profile_id)
            issues.append({
                "id": profile_id,
                "name": profile_id,
                "source": item_source,
                "reason": "Profile entry must be an object.",
            })
            continue
        try:
            normalize_service_profile(
                {**dict(item), **({"engine": "vllm"} if item_source == "legacy-vllm" else {})},
                index,
                source=item_source,
            )
        except (TypeError, ValueError) as exc:
            raw_id = _as_text(item.get("id"))
            profile_id = raw_id if _ID_RE.fullmatch(raw_id or "") else f"profile-{index + 1}"
            base_id = profile_id
            suffix = 2
            while profile_id in used_ids:
                profile_id = f"{base_id}-{suffix}"
                suffix += 1
            used_ids.add(profile_id)
            issues.append({
                "id": profile_id,
                "name": _as_text(item.get("name")) or profile_id,
                "source": item_source,
                "reason": str(exc),
            })
    return issues


def public_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Return the catalog-safe subset of a normalized profile."""

    result = {
        key: copy.deepcopy(profile[key])
        for key in (
            "id",
            "name",
            "engine",
            "mode",
            "model",
            "base_url",
            "endpoint",
            "api_type",
            "auto_complete_endpoint",
            "max_concurrency",
        )
        if key in profile
    }
    return result


def _without_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_secrets(item)
            for key, item in value.items()
            if str(key).casefold().replace("-", "_") not in _SECRET_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_without_secrets(item) for item in value]
    return value


def profile_fingerprint(profile: Mapping[str, Any]) -> str:
    scrubbed = _without_secrets(dict(profile))
    payload = json.dumps(scrubbed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _safe_import_id(old_id: str, fingerprint: str) -> str:
    slug = _SAFE_ID_RE.sub("-", old_id.strip()).strip("-._")[:52] or "profile"
    candidate = f"imported-{slug}-{fingerprint}"
    return candidate[:128]


def _profile_source(profile: Mapping[str, Any], old_id: str, fingerprint: str) -> dict[str, str]:
    existing = profile.get("imported_from")
    if isinstance(existing, Mapping):
        return {
            "source": _as_text(existing.get("source")) or "extension",
            "id": _as_text(existing.get("id")) or old_id,
            "fingerprint": _as_text(existing.get("fingerprint")) or fingerprint,
        }
    return {"source": "extension", "id": old_id, "fingerprint": fingerprint}


def import_profiles(
    path: str | os.PathLike[str],
    profiles: Sequence[Mapping[str, Any]],
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Idempotently import legacy extension profiles into shared storage.

    Invalid entries are reported individually and do not prevent valid entries
    from being committed.  The result deliberately contains no profile body,
    so an API key can never be echoed as part of an import acknowledgement.
    """

    if not isinstance(profiles, Sequence) or isinstance(profiles, (str, bytes)):
        raise ValueError("Profiles must be an array")
    config_path = _path_value(path)
    with _lock_for(config_path):
        with _process_lock(config_path):
            raw_current = _read_raw_document(config_path)
            embedded = raw_current.get(_STORE_META_KEY) if isinstance(raw_current.get(_STORE_META_KEY), Mapping) else None
            config = copy.deepcopy(raw_current)
            config.pop(_STORE_META_KEY, None)
            current_revision = _read_revision(config_path, config, embedded)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise RevisionConflictError(int(expected_revision), current_revision)
            updated = copy.deepcopy(config)
            translation = updated.setdefault("translation", {})
            if not isinstance(translation, MutableMapping):
                translation = {}
                updated["translation"] = translation
            existing_raw = translation.get("service_profiles")
            has_explicit_catalog = isinstance(existing_raw, list)
            if not isinstance(existing_raw, list):
                existing_raw = translation.get("profiles")
                has_explicit_catalog = isinstance(existing_raw, list)
            if not isinstance(existing_raw, list):
                existing_raw = []
            existing: list[dict[str, Any]] = []
            for index, item in enumerate(existing_raw):
                if not isinstance(item, Mapping):
                    continue
                try:
                    existing.append(normalize_service_profile(item, index, source=str(item.get("source") or "desktop")))
                except (TypeError, ValueError):
                    # Keep malformed old entries in place for GUI repair, but do
                    # not let them break future valid imports.
                    existing.append(copy.deepcopy(dict(item)))
            if not has_explicit_catalog:
                existing_ids = {
                    _as_text(item.get("id")) for item in existing if isinstance(item, Mapping)
                }
                for profile in _legacy_vllm_profiles(config):
                    profile_id = _as_text(profile.get("id"))
                    if profile_id and profile_id not in existing_ids:
                        existing.append(profile)
                        existing_ids.add(profile_id)

            mapping: dict[str, str] = {}
            errors: list[dict[str, Any]] = []
            changed = False
            for index, raw in enumerate(profiles):
                old_id = _as_text(raw.get("id")) if isinstance(raw, Mapping) else ""
                if not old_id:
                    old_id = f"entry-{index + 1}"
                try:
                    normalized = normalize_service_profile(raw, index, source="extension")
                    fingerprint = profile_fingerprint(normalized)
                # Match source and non-secret settings first. This gives a
                # retry the same catalog ID even if the supplied API key is
                # refreshed between attempts.
                    match = next(
                    (
                        item
                        for item in existing
                        if isinstance(item.get("imported_from"), Mapping)
                        and _as_text(item["imported_from"].get("source")) == "extension"
                        and _as_text(item["imported_from"].get("id")) == old_id
                        and _as_text(item["imported_from"].get("fingerprint")) == fingerprint
                    ),
                    None,
                )
                    if match is not None:
                        new_key = normalized.get("api_key", "")
                        if match.get("api_key") != new_key:
                            match["api_key"] = new_key
                            changed = True
                        mapping[old_id] = "profile:" + _as_text(match["id"])
                        continue
                    imported_id = _safe_import_id(old_id, fingerprint)
                    used = {_as_text(item.get("id")) for item in existing}
                    if imported_id in used:
                        # A hand-created profile may already occupy the stable
                        # generated ID. Preserve it and create a deterministic
                        # collision suffix from the full fingerprint.
                        imported_id = _safe_import_id(old_id, hashlib.sha256((fingerprint + old_id).encode()).hexdigest()[:20])
                    normalized["id"] = imported_id
                    normalized["imported_from"] = _profile_source(normalized, old_id, fingerprint)
                    existing.append(normalized)
                    mapping[old_id] = "profile:" + imported_id
                    changed = True
                except (TypeError, ValueError, OverflowError) as exc:
                    # Error strings contain only validation text and the caller's
                    # stable ID; never include the raw profile or credential.
                    errors.append({"id": old_id, "index": index, "error": str(exc)})

            if changed:
                translation["service_profiles"] = existing
                new_revision = _next_revision(current_revision)
                clean_updated = copy.deepcopy(updated)
                updated[_STORE_META_KEY] = {
                    "revision": new_revision,
                    "digest": _document_digest(clean_updated),
                }
                _atomic_write(config_path, _dump_document(config_path, updated))
                _write_revision(config_path, new_revision, clean_updated)
            else:
                new_revision = current_revision
            return {"revision": new_revision, "mapping": mapping, "errors": errors}


def server_id_for_config(path: str | os.PathLike[str], config: Mapping[str, Any] | None = None) -> str:
    """Return a stable identity scoped to one configured server.

    Explicit ``server.server_id`` wins.  For older configs the resolved config
    path is the server scope, so two servers on the same URL but backed by
    different config files do not share extension cache keys.  The UUID5 is
    deterministic and requires no startup write to the repository's default
    config file.
    """

    if isinstance(config, Mapping):
        server = config.get("server")
        if isinstance(server, Mapping):
            configured = _as_text(server.get("server_id"))
            if configured:
                return configured
    resolved = str(_path_value(path).absolute()).casefold()
    return "cfg-" + uuid.uuid5(uuid.NAMESPACE_URL, "stt-tts:" + resolved).hex
