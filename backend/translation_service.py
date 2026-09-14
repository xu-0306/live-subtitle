"""Extension-selected translation leases and on-demand managed runtime ownership.

One provider/model can serve multiple captures/languages. A different provider
must wait for every old capture to stop; no silent route fallback is permitted.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import threading
from pathlib import Path

from .config_store import public_profile, server_id_for_config, service_profile_issues, service_profiles
from .local_catalog import get_model, list_models
from .local_runtime import ManagedRuntime, detect_hardware, preflight, prepare_install
from .model_manager import (
    WHISPER_MODEL_URLS,
    resolve_stt_model_target,
)


def _profile_for_selection(config: dict, profile_id: str) -> dict | None:
    for profile in service_profiles(config):
        if str(profile.get("id") or "") == profile_id:
            return profile
    return None


def _profile_translation_config(base: dict, profile: dict) -> dict:
    """Adapt a normalized service profile to the existing translator config.

    The translator implementations deliberately keep their established
    provider-specific sections.  This adapter is the only place where the
    service-neutral catalog representation becomes that legacy runtime shape.
    """

    result = copy.deepcopy(base)
    engine = str(profile.get("engine") or "").strip().lower()
    result["engine"] = engine
    result["profile_id"] = str(profile.get("id") or "")
    options = profile.get("options") if isinstance(profile.get("options"), dict) else {}
    if engine == "ollama":
        nested = options.get("ollama") if isinstance(options.get("ollama"), dict) else {}
        result["ollama"] = {
            **copy.deepcopy(nested),
            "model": str(profile.get("model") or nested.get("model") or ""),
            "host": str(profile.get("base_url") or nested.get("host") or ""),
        }
    elif engine == "nllb":
        nested = options.get("nllb") if isinstance(options.get("nllb"), dict) else {}
        result["nllb"] = {
            **copy.deepcopy(nested),
            "model": str(profile.get("model") or nested.get("model") or ""),
        }
    elif engine in {"vllm", "openai", "openai_compatible"}:
        nested_key = "vllm" if engine == "vllm" else "openai"
        nested = options.get(engine) if isinstance(options.get(engine), dict) else options.get(nested_key)
        if not isinstance(nested, dict):
            nested = {}
        provider = {
            **copy.deepcopy(nested),
            "id": str(profile.get("id") or nested.get("id") or ""),
            "profile_id": str(profile.get("id") or nested.get("profile_id") or ""),
            "name": str(profile.get("name") or nested.get("name") or ""),
            "base_url": str(profile.get("base_url") or nested.get("base_url") or ""),
            "api_key": str(profile.get("api_key") or nested.get("api_key") or ""),
            "model": str(profile.get("model") or nested.get("model") or ""),
            "served_model": str(profile.get("model") or nested.get("served_model") or ""),
            "api_type": str(profile.get("api_type") or nested.get("api_type") or "chat_completions"),
            "auto_complete_endpoint": bool(profile.get("auto_complete_endpoint", True)),
            "max_concurrency": profile.get("max_concurrency", nested.get("max_concurrency", 0)),
            "mode": str(profile.get("mode") or nested.get("mode") or "external"),
        }
        if engine == "vllm":
            result["vllm"] = {"active_profile": provider["id"], "profiles": [provider]}
        else:
            # The existing translator uses the ``openai`` section for both
            # OpenAI and generic OpenAI-compatible profiles.  Keep the
            # canonical engine on the top-level config while adapting the
            # provider payload to that established runtime section.
            result["openai"] = provider
            if engine == "openai_compatible":
                result["openai_compatible"] = provider
    elif engine == "managed_llama":
        result["managed_llama"] = copy.deepcopy(options.get("managed_llama") or {})
    result["selection"] = {"kind": "profile", "id": str(profile.get("id") or "")}
    return result


def _default_translation_selection(config: dict) -> dict | None:
    translation = config.get("translation")
    if not isinstance(translation, dict):
        return None
    for key in ("default_selection", "selection"):
        selection = translation.get(key)
        if isinstance(selection, dict) and selection.get("kind") and selection.get("id"):
            return {"kind": str(selection["kind"]), "id": str(selection["id"])}
    engine = str(translation.get("engine") or "").strip().lower()
    if engine == "managed_llama":
        local = config.get("local_llama")
        if isinstance(local, dict):
            model_id = str(local.get("model_id") or "").strip()
            if model_id:
                return {"kind": "local", "id": model_id}
    if engine == "vllm":
        vllm = translation.get("vllm")
        if isinstance(vllm, dict):
            profile_id = str(vllm.get("active_profile") or "").strip()
            if profile_id and _profile_for_selection(config, profile_id):
                return {"kind": "profile", "id": profile_id}
    profile_id = str(
        translation.get("active_profile")
        or translation.get("active_service")
        or translation.get("profile_id")
        or translation.get("default_profile_id")
        or ""
    ).strip()
    if profile_id and _profile_for_selection(config, profile_id):
        return {"kind": "profile", "id": profile_id}
    if engine in {"ollama", "nllb", "openai", "openai_compatible"}:
        # A config migrated from the extension may only have engine-specific
        # settings. Match an equivalent normalized profile when one exists.
        candidates = service_profiles(config)
        for profile in candidates:
            if profile.get("engine") == engine:
                return {"kind": "profile", "id": str(profile.get("id"))}
    return None


def resolve_selection(config: dict, update: dict | None) -> tuple[dict, str | None]:
    if update is not None and not isinstance(update, dict):
        raise ValueError('Translation settings must be an object')
    supplied_update = copy.deepcopy(update or {})
    update = copy.deepcopy(supplied_update)
    base = copy.deepcopy(config.get('translation') or {'engine': 'noop'})
    selection = update.pop('selection', None)
    # A v2 capture may omit translation.selection to explicitly inherit the
    # desktop default.  Legacy callers that send an engine/provider mapping
    # retain their historical meaning and are not rewritten underneath them.
    if selection is None and 'engine' not in supplied_update and not any(
        key in supplied_update for key in ('openai', 'openai_compatible', 'ollama', 'nllb', 'vllm')
    ):
        selection = _default_translation_selection(config)
    local_id = None
    if selection is not None:
        if not isinstance(selection, dict):
            raise ValueError('Invalid translation selection')
        kind, identity = selection.get('kind'), selection.get('id')
        # Adapter boundary: IDs reference server-side settings; clients never
        # receive the stored API keys or supply managed process parameters.
        if kind == 'none':
            # The extension's None option is an explicit no-translation route,
            # not a provider lease.  It may coexist with an active model.
            base['engine'] = 'noop'
            base.pop('selection', None)
            local_id = None
        elif kind == 'local':
            local = config.get('local_llama', {})
            installed = local.get('installed_models', [])
            legacy = local.get('model_id') if local.get('enabled') else None
            if identity not in installed and identity != legacy:
                raise ValueError('Local model is not installed. Download it in the desktop app first.')
            get_model(identity, config)
            local_id = identity
            base['engine'] = 'managed_llama'
            base.pop('managed_llama', None)
        elif kind == 'profile':
            profile = _profile_for_selection(config, identity)
            if profile is None:
                raise ValueError('Translation service profile no longer exists. Refresh desktop services.')
            base = _profile_translation_config(base, profile)
            base['engine'] = str(profile.get('engine') or base.get('engine') or 'vllm')
        else:
            raise ValueError('Unknown translation selection kind')
        for key in ('target_language', 'partial'):
            if key in update:
                base[key] = update[key]
        base['selection'] = selection
    else:
        for key, value in update.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key].update(value)
            else:
                base[key] = value
        if base.get('engine') == 'managed_llama':
            local_id = config.get('local_llama', {}).get('model_id')
            if not local_id:
                raise ValueError('Choose an installed local model in the extension')
    target = str(base.get('target_language', config.get('local_llama', {}).get('target_language', 'zh-TW'))).strip()
    if not target or len(target) > 128 or any(ord(c) < 32 for c in target):
        raise ValueError('Enter a target language name or language code (1–128 characters)')
    base['target_language'] = target
    if str(base.get('engine', 'noop')) not in {'noop', 'managed_llama', 'vllm', 'openai', 'openai_compatible', 'ollama', 'nllb'}:
        raise ValueError('Unsupported translation engine')
    return base, local_id


def provider_key(cfg: dict, local_id: str | None) -> str | None:
    engine = cfg.get('engine', 'noop')
    if engine == 'noop' and not cfg.get('browser_profile'):
        return None
    if local_id:
        data = ['managed_llama', local_id]
    elif engine == 'vllm':
        section = cfg.get('vllm', {})
        profile = next((p for p in section.get('profiles', []) if p.get('id') == section.get('active_profile')), section)
        data = [engine, profile]
    else:
        data = [engine, cfg.get(engine, {}), cfg.get('browser_profile')]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class TranslationService:
    def __init__(
        self,
        get_config,
        root: Path,
        runtime=None,
        on_idle=None,
        *,
        config_path: Path | None = None,
        revision_provider=None,
        server_id: str | None = None,
        instance_id: str | None = None,
        snapshot_provider=None,
    ):
        self.get_config, self.root = get_config, root
        self.config_path = Path(config_path) if config_path is not None else None
        self.revision_provider = revision_provider
        self.snapshot_provider = snapshot_provider
        self.server_id = server_id
        self.instance_id = instance_id
        self.runtime = runtime or ManagedRuntime()
        self.lock = asyncio.Lock()
        self.leases: dict[str, str | None] = {}
        self.active_key = None
        self.local_result = None
        self.active_name = None
        self.active_selection: dict[str, str] | None = None
        self.on_idle = on_idle

    def catalog(self) -> dict:
        """Legacy v1 translation catalog retained for older extensions."""
        config = self.get_config()
        local = config.get('local_llama', {})
        installed = set(local.get('installed_models', []))
        if local.get('enabled') and local.get('model_id'):
            installed.add(local['model_id'])
        options = [dict(id='local:' + m.id, name=m.name, engine='desktop',
                        selection={'kind': 'local', 'id': m.id})
                   for m in list_models(config) if m.id in installed]
        options += [dict(id='profile:' + p['id'], name=p.get('name') or p['id'], engine='desktop',
                         selection={'kind': 'profile', 'id': p['id']})
                    for p in service_profiles(config)
                    if p.get('id') and p.get('base_url') and p.get('model')]
        return {'type': 'translation_catalog', 'version': 1, 'models': options,
                'active': self.active_name, 'captures': len(self.leases)}

    @staticmethod
    def _file_status(target: Path | None) -> tuple[bool, str, str | None]:
        if target is None:
            return False, 'missing', 'No local model path is configured.'
        try:
            if target.is_file() and target.stat().st_size > 0:
                return True, 'installed', None
        except OSError:
            pass
        return False, 'missing', 'Model file is not present.'

    def _local_model_state(self, config: dict, model) -> tuple[bool, str, str | None]:
        local = config.get('local_llama') if isinstance(config.get('local_llama'), dict) else {}
        configured_root = str(local.get('root') or '').strip()
        model_root = Path(configured_root).expanduser() if configured_root else self.root
        root = model_root / 'models' / model.id
        try:
            paths = [root / artifact.filename for artifact in model.files]
            if all(path.is_file() and path.stat().st_size == artifact.size for path, artifact in zip(paths, model.files)):
                return True, 'installed', None
            if any(path.exists() for path in paths):
                return False, 'invalid', 'One or more model artifacts are missing or incomplete.'
        except OSError:
            return False, 'invalid', 'Model files could not be inspected.'
        if model.id in set(local.get('installed_models') or []):
            return False, 'missing', 'Model is selected but its files are not installed.'
        return False, 'missing', 'Model is not installed.'

    def _stt_catalog(self, config: dict) -> list[dict]:
        stt = config.get('stt') if isinstance(config.get('stt'), dict) else {}
        cache_dir_raw = stt.get('model_cache_dir')
        cache_dir = Path(str(cache_dir_raw)).expanduser() if cache_dir_raw else None
        entries: list[dict] = []
        known: dict[str, dict] = {}

        def add(model_id: str, name: str, backend: str | None, target: Path | None):
            available, status, reason = self._file_status(target)
            selection = {'model': model_id}
            if backend:
                selection['backend'] = backend
            entry = {'id': model_id, 'name': name, 'available': available, 'status': status, 'selection': selection}
            if reason:
                entry['reason'] = reason
            previous = known.get(model_id)
            if previous is None:
                known[model_id] = entry
                entries.append(entry)
            elif not previous.get('available') and entry.get('available'):
                # A configured explicit path may make a built-in ID available
                # even when the default cache location is empty. Keep one
                # stable ID and expose the useful status.
                previous.clear()
                previous.update(entry)

        for model_id in WHISPER_MODEL_URLS:
            model_cfg = {'model': model_id, 'model_cache_dir': str(cache_dir) if cache_dir else None}
            add(model_id, 'Whisper ' + model_id, None, resolve_stt_model_target(model_cfg))
        if cache_dir and cache_dir.is_dir():
            try:
                for path in sorted(cache_dir.glob('*.pt'), key=lambda value: value.name.casefold()):
                    add(path.stem, path.stem, None, path)
            except OSError:
                pass
        configured_model = str(stt.get('model') or '').strip()
        configured_backend = str(stt.get('backend') or '').strip() or None
        if configured_model:
            # Re-check the configured target even for a built-in ID.  A
            # ``model_path`` outside the cache directory is a supported
            # deployment shape and must be allowed to upgrade the built-in
            # catalog entry from missing to installed.
            target = resolve_stt_model_target(stt)
            add(configured_model, configured_model, configured_backend, target)
        return entries

    def _profile_catalog_state(self, profile: dict) -> tuple[bool, str, str | None]:
        engine = str(profile.get('engine') or '').lower()
        model = str(profile.get('model') or '').strip()
        endpoint = str(profile.get('base_url') or '').strip()
        if engine == 'nllb':
            return (bool(model), 'configured' if model else 'invalid', None if model else 'NLLB model is not configured.')
        if engine == 'managed_llama':
            return False, 'unavailable', 'Managed runtime is controlled by the desktop process.'
        if not model:
            return False, 'invalid', 'Translation service model is not configured.'
        if engine not in {'ollama', 'openai', 'openai_compatible', 'vllm'}:
            return False, 'unsupported', 'Translation service engine is not supported.'
        if not endpoint:
            return False, 'invalid', 'Translation service endpoint is not configured.'
        return True, 'configured', None

    def _revision(self, config: dict, revision: int | None = None) -> int:
        if revision is not None:
            return int(revision)
        if callable(self.revision_provider):
            try:
                return int(self.revision_provider())
            except (TypeError, ValueError, OSError):
                pass
        if self.config_path is not None:
            try:
                from .config_store import load_snapshot

                _, revision = load_snapshot(self.config_path)
                return int(revision)
            except (OSError, ValueError, TypeError):
                pass
        return 0

    def desktop_catalog(
        self,
        *,
        server_id: str | None = None,
        instance_id: str | None = None,
        config_override: dict | None = None,
        revision_override: int | None = None,
    ) -> dict:
        """Build the side-effect-free v2 catalog consumed by GUI/extension."""

        supplied_revision = revision_override
        if isinstance(config_override, dict):
            config = copy.deepcopy(config_override)
        elif callable(self.snapshot_provider):
            try:
                snapshot = self.snapshot_provider()
                if isinstance(snapshot, tuple) and len(snapshot) == 2 and isinstance(snapshot[0], dict):
                    config, supplied_revision = snapshot
                else:
                    config = snapshot
            except (OSError, TypeError, ValueError):
                config = self.get_config()
        else:
            config = self.get_config()
        if not isinstance(config, dict):
            config = {}
        local = config.get('local_llama') if isinstance(config.get('local_llama'), dict) else {}
        installed = set(local.get('installed_models') or [])
        if local.get('enabled') and local.get('model_id'):
            installed.add(str(local['model_id']))
        models: list[dict] = []
        for model in list_models(config):
            if model.id not in installed:
                continue
            available, status, reason = self._local_model_state(config, model)
            entry = {
                'id': 'local:' + model.id,
                'name': model.name,
                'engine': 'desktop',
                'selection': {'kind': 'local', 'id': model.id},
                'available': available,
                'status': status,
            }
            if reason:
                entry['reason'] = reason
            models.append(entry)
        for profile in service_profiles(config):
            profile_id = str(profile.get('id') or '').strip()
            if not profile_id:
                continue
            available, status, reason = self._profile_catalog_state(profile)
            entry = {
                'id': 'profile:' + profile_id,
                'name': str(profile.get('name') or profile_id),
                'engine': 'desktop',
                'selection': {'kind': 'profile', 'id': profile_id},
                'available': available,
                'status': status,
            }
            if reason:
                entry['reason'] = reason
            models.append(entry)
        known_profile_ids = {
            str(profile.get('id') or '').strip() for profile in service_profiles(config)
        }
        for issue in service_profile_issues(config):
            profile_id = str(issue.get('id') or '').strip()
            if not profile_id or profile_id in known_profile_ids:
                continue
            models.append({
                'id': 'profile:' + profile_id,
                'name': str(issue.get('name') or profile_id),
                'engine': 'desktop',
                'selection': {'kind': 'profile', 'id': profile_id},
                'available': False,
                'status': 'invalid',
                'reason': str(issue.get('reason') or 'Profile settings are invalid.'),
            })

        stt_cfg = config.get('stt') if isinstance(config.get('stt'), dict) else {}
        stt_default = {'model': str(stt_cfg.get('model') or 'medium')}
        if stt_cfg.get('backend'):
            stt_default['backend'] = str(stt_cfg['backend'])
        translation_cfg = config.get('translation') if isinstance(config.get('translation'), dict) else {}
        translation_default = {
            'target_language': str(translation_cfg.get('target_language') or local.get('target_language') or 'zh-TW'),
            'partial': bool(translation_cfg.get('partial', False)),
        }
        selection = _default_translation_selection(config)
        if selection:
            translation_default['selection'] = selection
        active = {'name': self.active_name, 'captures': len(self.leases)}
        if self.active_selection:
            active['selection'] = copy.deepcopy(self.active_selection)
        catalog_server_id = server_id or self.server_id
        if not catalog_server_id:
            identity_path = self.config_path or (self.root / 'config.yaml')
            catalog_server_id = server_id_for_config(identity_path, config)
        result = {
            'type': 'desktop_catalog',
            'version': 2,
            'server_id': catalog_server_id,
            'instance_id': instance_id or self.instance_id or '',
            'revision': self._revision(config, supplied_revision),
            'models': models,
            'stt_models': self._stt_catalog(config),
            'defaults': {'stt': stt_default, 'translation': translation_default},
            'active': active,
            'capabilities': ['profile_import'],
        }
        return result

    async def acquire(
        self,
        session_id: str,
        update: dict | None,
        stt: dict,
        *,
        config_override: dict | None = None,
    ) -> dict:
        async with self.lock:
            config = copy.deepcopy(config_override) if isinstance(config_override, dict) else self.get_config()
            cfg, local_id = resolve_selection(config, update)
            key = provider_key(cfg, local_id)
            if key and self.active_key and key != self.active_key:
                raise ValueError('Another translation model is in use. Stop existing captures before switching local/cloud services.')
            if session_id in self.leases and key != self.leases[session_id]:
                raise ValueError('Stop this capture before changing translation services.')
            if local_id:
                if self.local_result is None:
                    settings = dict(config.get('local_llama', {}))
                    model = get_model(local_id, config)
                    settings['disable_thinking'] = model.disable_thinking
                    settings['target_language'] = cfg['target_language']
                    cancel = threading.Event()
                    def start():
                        preflight(model, detect_hardware(), settings, stt)
                        kind = 'windows-cpu' if settings.get('gpu_layers') == 0 else 'windows-cuda'
                        exe, weights = prepare_install(self.root, model, kind, cancel, lambda *a: None,
                                                       allow_download=False)
                        return self.runtime.start(exe, weights, model.id, settings, cancel,
                                                  self.root / 'runtime.log', lambda *a: None)
                    task = asyncio.create_task(asyncio.to_thread(start))
                    try:
                        self.local_result = await asyncio.shield(task)
                    except BaseException:
                        cancel.set()
                        await asyncio.to_thread(self.runtime.stop)
                        await asyncio.gather(task, return_exceptions=True)
                        self.local_result = None
                        raise
                if not self.runtime.alive():
                    raise RuntimeError('Local translation runtime stopped. Stop captures and retry.')
                cfg['managed_llama'] = {**self.local_result, 'max_concurrency': 1, 'max_tokens': 192}
            self.leases[session_id] = key
            if key:
                self.active_key = key
                self.active_name = local_id or cfg.get('engine')
                if local_id:
                    self.active_selection = {'kind': 'local', 'id': local_id}
                else:
                    selected = cfg.get('selection')
                    if isinstance(selected, dict) and selected.get('kind') and selected.get('id'):
                        self.active_selection = {
                            'kind': str(selected['kind']),
                            'id': str(selected['id']),
                        }
                    else:
                        profile_id = str(cfg.get('profile_id') or '').strip()
                        self.active_selection = (
                            {'kind': 'profile', 'id': profile_id} if profile_id else None
                        )
            return cfg

    async def release(self, session_id: str) -> None:
        async with self.lock:
            self.leases.pop(session_id, None)
            if not any(self.leases.values()):
                # Terminate and reap owned inference before a cloud route is allowed.
                await asyncio.to_thread(self.runtime.stop)
                if self.on_idle:
                    await self.on_idle()
                self.local_result = self.active_key = self.active_name = None
                self.active_selection = None

    async def close(self):
        async with self.lock:
            await asyncio.to_thread(self.runtime.stop)
            self.leases.clear()
            self.local_result = self.active_key = self.active_name = None
            self.active_selection = None
