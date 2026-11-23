"""Main-owned Qt lifecycle controller for the passive local model form."""
from __future__ import annotations

import copy
import html
import threading
from dataclasses import asdict
from pathlib import Path

from PySide6 import QtCore, QtWidgets
from qfluentwidgets import BodyLabel, LineEdit, StrongBodyLabel

from backend.local_catalog import (
    MODELS,
    RUNTIMES,
    get_model,
    list_models,
    model_artifact_state,
    resolve_hf_model,
)
from backend.local_runtime import (
    Cancelled,
    ManagedRuntime,
    auto_gpu_fit_target_mb,
    detect_hardware,
    preflight,
    prepare_install,
    runtime_kind,
    uses_vulkan,
)
from gui import config_manager
from gui._local_model_form import RUNTIME_DEFAULTS, build_form
from gui.fluent_kit import FluentDialog, muted_label


def runtime_placement_text(placement: dict | None) -> str:
    if not placement:
        return 'Placement unknown'
    vram = f'{placement["vram_mb"] / 1024:.1f} GiB VRAM'
    return {
        'gpu': f'Running on GPU · {vram}',
        'partial': f'Partly on GPU · {vram} · translation will be slower. '
                   'Try a smaller model or free VRAM.',
        'cpu': 'Running on CPU',
    }.get(placement.get('state'), 'Placement unknown')


def _pin_legacy_local_default(cfg: dict) -> None:
    """Keep a v0.2.0 default (engine=managed_llama) as a real selection before the engine is retired."""
    translation = cfg.get('translation')
    model_id = (cfg.get('local_llama') or {}).get('model_id')
    if not (isinstance(translation, dict) and model_id and translation.get('engine') == 'managed_llama'):
        return
    if not any(isinstance(s, dict) and s.get('kind') and s.get('id')
               for s in (translation.get('default_selection'), translation.get('selection'))):
        translation['default_selection'] = {'kind': 'local', 'id': model_id}


def activate_config(config: dict, model_id: str, settings: dict, result: dict) -> dict:
    updated = copy.deepcopy(config)
    _pin_legacy_local_default(updated)
    local = updated.setdefault('local_llama', {})
    # Installation publishes availability only; the extension chooses inference.
    installed = list(local.get('installed_models', []))
    if local.get('enabled') and local.get('model_id') not in installed:
        installed.append(local['model_id'])
    if model_id not in installed:
        installed.append(model_id)
    local.update(settings)
    local.update(enabled=False, model_id=model_id, installed_models=installed)
    translation = updated.setdefault('translation', {})
    if local.get('pending_default_model_id') == model_id:
        translation['default_selection'] = {'kind': 'local', 'id': model_id}
        local.pop('pending_default_model_id', None)
    if translation.get('engine') == 'managed_llama':
        translation['engine'] = 'noop'
    translation.pop('managed_llama', None)  # Never persist temporary runtime tokens.
    local.pop('previous_translation', None)
    return updated


def deactivate_config(config: dict) -> dict:
    updated = copy.deepcopy(config)
    local = updated.setdefault('local_llama', {})
    local['installed_models'] = [m for m in local.get('installed_models', []) if m != local.get('model_id')]
    local['enabled'] = False
    translation = updated.get('translation', {})
    if translation.get('default_selection') == {'kind': 'local', 'id': local.get('model_id')}:
        translation['default_selection'] = {'kind': 'none', 'id': 'none'}
    if translation.get('engine') == 'managed_llama':
        translation['engine'] = 'noop'
    return updated


class AddModelDialog(FluentDialog):
    """Ask for a public Hugging Face repository and GGUF file name."""

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.widget.setMinimumWidth(480)
        self.viewLayout.addWidget(StrongBodyLabel('Add public Hugging Face GGUF'))
        form = QtWidgets.QFormLayout()
        form.setVerticalSpacing(10)
        self.repo, self.filename = LineEdit(), LineEdit()
        self.repo.setPlaceholderText('owner/repository')
        self.filename.setPlaceholderText('model-Q4_K_M.gguf (or first shard path)')
        form.addRow(BodyLabel('Repository'), self.repo)
        form.addRow(BodyLabel('GGUF file'), self.filename)
        self.viewLayout.addLayout(form)
        self.viewLayout.addWidget(muted_label(
            'Public GGUF weights only. The model must support a standard chat template. '
            'A pinned revision and SHA256 will be saved; no repository code runs.', small=True))
        buttons = self.set_buttons([('Cancel', False, self.reject), ('OK', True, self.accept)])
        buttons['OK'].setDefault(True)


class InstallWorker(QtCore.QThread):
    progress = QtCore.Signal(str, object, object)
    hardware = QtCore.Signal(str)
    hardware_warning = QtCore.Signal(str)
    outcome = QtCore.Signal(object, str)

    def __init__(self, root: Path, model_id: str, settings: dict, stt: dict,
                 runtime: ManagedRuntime, allow_download: bool, config: dict | None = None) -> None:
        super().__init__()
        self.config = config or {}
        self.root, self.model_id = root, model_id
        self.settings, self.stt = dict(settings), dict(stt)
        self.runtime = runtime
        self.allow_download = allow_download
        self.cancel_event = threading.Event()

    def run(self) -> None:
        try:
            model = get_model(self.model_id, self.config)
            hw = detect_hardware()
            try:
                self.hardware.emit(preflight(model, hw, self.settings, self.stt))
            except RuntimeError as exc:
                self.hardware.emit(str(exc) + ' Download is allowed; capture checks memory again.')
                self.hardware_warning.emit('GPU memory may be insufficient. Try a smaller model or CPU mode. Download can continue.')
            kind = runtime_kind(self.settings['gpu_layers'], hw)
            executable, model_path = prepare_install(self.root, model, kind, self.cancel_event,
                                                     self.progress.emit, allow_download=self.allow_download)
            if self.cancel_event.is_set():
                raise Cancelled('Download cancelled; completed files were kept.')
            self.outcome.emit({'installed': True}, '')
        except Exception as exc:
            self.runtime.stop()
            label = str(exc) if isinstance(exc, (Cancelled, RuntimeError, ValueError)) else f'{type(exc).__name__}: {exc}'
            self.outcome.emit(None, label)

    def cancel(self) -> None:
        self.cancel_event.set()
        self.runtime.stop()


class LocalModelController(QtCore.QObject):
    activated = QtCore.Signal(object)
    deactivated = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, widget: QtWidgets.QWidget, root: Path, get_config, persist_config, parent=None) -> None:
        super().__init__(parent)
        self.root = root
        self.get_config = get_config
        self.persist_config = persist_config
        self.controls = build_form(widget, [{'id': m.id, 'name': m.name} for m in list_models(self.get_config())])
        for name in ('context', 'gpu_layers', 'port'):
            self.controls[name] = self.controls['advanced'].findChild(QtWidgets.QSpinBox, name)
        self.detected_hardware = detect_hardware()
        self.runtime = ManagedRuntime()
        self.worker: InstallWorker | None = None
        self.closing = False
        self._pending_result = None
        self._pending_error = ''
        self.controls['hardware'].setText('Downloads do not load the GPU. Captures check the free GPU memory first. Too little: use CPU or an external service.')
        self.controls['add_model'].clicked.connect(self.add_model)
        self.controls['install'].clicked.connect(self.install)
        self.controls['cancel'].clicked.connect(self.cancel)
        self.controls['stop'].clicked.connect(self.stop)
        self.controls['model'].currentIndexChanged.connect(self._details)
        self.controls['gpu_layers'].valueChanged.connect(self._details)
        # Main loads settings after all application controls have been initialized.
        self._details()
        self.catalog_worker = None
        self.watchdog = QtCore.QTimer(self)
        self.watchdog.setInterval(2000)
        self.watchdog.timeout.connect(self._check_runtime)
        self.watchdog.start()

    def load_config(self, cfg: dict) -> None:
        if self.busy or self.runtime.alive():
            return
        local = cfg.get('local_llama', {})
        storage = cfg.get('models') if isinstance(cfg.get('models'), dict) else {}
        storage_root = str(storage.get('root') or '').strip()
        configured_root = (
            str(
                Path(storage_root).expanduser()
                / config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME
            )
            if storage_root
            else str(local.get('root') or '').strip()
        )
        if configured_root:
            self.root = Path(configured_root).expanduser()
        for key, default in RUNTIME_DEFAULTS.items():
            control = self.controls[key]
            try:
                value = int(local.get(key, default))
            except (ValueError, TypeError):
                value = default
            if hasattr(control, 'setValue'):
                control.setValue(value)
            else:
                index = control.findData(value)
                control.setCurrentIndex(index if index >= 0 else control.findData(default))
        self._refresh_models(local.get('model_id', MODELS[0].id))
        index = self.controls['model'].findData(local.get('model_id', MODELS[0].id))
        if index >= 0:
            self.controls['model'].setCurrentIndex(index)
        self._set_busy(False)

    def set_root(self, root: Path) -> bool:
        """Point future downloads at the shared model-storage location."""

        if self.busy or self.runtime.alive():
            return False
        self.root = Path(root)
        return True

    def refresh_state(self) -> None:
        if not self.busy and not self.runtime.alive():
            self._details()

    def show_runtime_placement(self, runtime: dict | None, prefix: str = '') -> None:
        """Where the running model's weights sit; ``runtime`` is a start result or health entry."""
        text = runtime_placement_text(runtime.get('placement')) if runtime is not None else ''
        if text and prefix:
            text = f'{prefix}{text}'
        self.controls['placement'].setText(text)
        self.controls['placement'].setVisible(bool(text))

    def _runtime_settings(self) -> dict:
        settings = {}
        for key in RUNTIME_DEFAULTS:
            control = self.controls[key]
            settings[key] = control.value() if hasattr(control, 'value') else control.currentData()
        return settings

    @property
    def busy(self) -> bool:
        return self.worker is not None

    def _selected_model_state(self):
        model = get_model(self.controls['model'].currentData(), self.get_config())
        available, status, reason = model_artifact_state(self.root, model)
        published = model.id in set(
            self.get_config().get('local_llama', {}).get('installed_models', [])
        )
        return model, available, published, status, reason

    def _details(self, *_args) -> None:
        model, available, published, _status, reason = self._selected_model_state()
        self.controls['stop'].setEnabled(not self.busy and available and published)
        kind = runtime_kind(self.controls['gpu_layers'].value(), self.detected_hardware)
        runtime_size = sum(a.size for a in RUNTIMES[kind])
        self.controls['details'].setText(
            f'<b>{html.escape(model.name)}</b> · {model.size / 1024**3:.2f} GiB download'
        )
        self.controls['technical_details'].setText(
            f'{runtime_size / 1024**2:.0f} MiB runtime download on first use. '
            f'{html.escape(model.license)} · Runs locally from {html.escape(model.repo)}. '
            'Quality varies by language and content. The download includes every required GGUF part. '
            f'{html.escape(model.notes)}'
        )
        gpu_layers = self.controls['gpu_layers'].value()
        stt = self.get_config().get('stt', {})
        if gpu_layers == -1:
            fit_target = auto_gpu_fit_target_mb(stt, self.detected_hardware)
            kept_for = (
                'shared peaks; speech recognition runs on the CPU with this GPU (Vulkan)'
                if uses_vulkan(self.detected_hardware)
                else f'Whisper {stt.get("model", "medium")} and shared peaks'
            )
            offload_summary = (
                'Auto · llama.cpp chooses the highest fitting GPU layer count at each model load. '
                f'It keeps about {fit_target / 1024:.1f} GiB free for {kept_for}.'
            )
        elif gpu_layers == 0:
            offload_summary = 'CPU · Translation does not offload model layers to the GPU.'
        else:
            offload_summary = (
                f'Manual · llama.cpp will offload at most {gpu_layers} layer(s). '
                'If that does not fit, capture reports the load error instead of silently changing the value.'
            )
        self.controls['gpu_layers_summary'].setText(offload_summary)
        if not self.busy:
            self.controls['hardware_warning'].hide()
            try:
                self.controls['hardware'].setText(
                    preflight(
                        model,
                        self.detected_hardware,
                        {'gpu_layers': gpu_layers},
                        stt,
                    )
                )
            except (RuntimeError, ValueError) as exc:
                self.controls['hardware'].setText(str(exc))
            progress = self.controls['progress']
            if available:
                progress.setRange(0, 100)
                progress.setValue(100)
                progress.show()
                if published:
                    self.controls['install'].setText('Verify installation')
                    self.controls['status'].setText(
                        'Ready. Select this model in the extension to translate.'
                    )
                else:
                    self.controls['install'].setText('Add to extension choices')
                    self.controls['status'].setText(
                        'Model files are installed but hidden from new extension choices.'
                    )
            else:
                progress.setRange(0, 100)
                progress.setValue(0)
                progress.hide()
                self.controls['install'].setText('Download model')
                if published:
                    self.controls['status'].setText(
                        'Model files are missing or incomplete. Download again to use this model.'
                    )
                else:
                    self.controls['status'].setText(
                        (reason or 'Model files are not installed.') + ' Download to add it to extension choices.'
                    )

    def _set_busy(self, busy: bool) -> None:
        for key in ('model', 'advanced', 'install', 'add_model'):
            self.controls[key].setEnabled(not busy and not self.runtime.alive())
        self.controls['cancel'].setEnabled(busy)
        _model, available, published, _status, _reason = self._selected_model_state()
        self.controls['stop'].setEnabled(not busy and available and published)

    def install(self, _checked=False, *, allow_download=True) -> None:
        if self.busy or self.runtime.alive() or self.closing:
            return
        self._pending_result, self._pending_error = None, ''
        settings = self._runtime_settings()
        settings['root'] = str(self.root)
        worker =InstallWorker(self.root, self.controls['model'].currentData(), settings,
                               self.get_config().get('stt', {}), self.runtime, allow_download, self.get_config())
        self.worker = worker
        worker.progress.connect(self._progress)
        worker.hardware.connect(self.controls['hardware'].setText)
        worker.hardware_warning.connect(self._hardware_warning)
        worker.outcome.connect(self._outcome)
        worker.finished.connect(self._finished)
        self._set_busy(True)
        self._progress('Preparing local translation', 0, 0)
        worker.start()

    def _progress(self, message: str, done: int, total: int) -> None:
        bar = self.controls['progress']
        bar.show()
        bar.setRange(0, 100 if total else 0)
        if total:
            bar.setValue(min(100, int(done * 100 / total)))
        self.controls['status'].setText(message)

    def _hardware_warning(self, message: str) -> None:
        self.controls['hardware_warning'].setText(message)
        self.controls['hardware_warning'].setVisible(bool(message))

    def _outcome(self, result, error: str) -> None:
        self._pending_result, self._pending_error = result, error

    def _finished(self) -> None:
        worker, self.worker = self.worker, None
        if worker is None:
            return
        worker.deleteLater()
        result = self._pending_result
        try:
            if self.closing or worker.cancel_event.is_set():
                self.runtime.stop()
                self.controls['status'].setText('Cancelled. Downloaded files were kept.')
            elif self._pending_error:
                self.controls['status'].setText(self._pending_error)
                self.failed.emit(self._pending_error)
            elif result is not None:
                cfg = activate_config(self.get_config(), worker.model_id, worker.settings, result)
                self.persist_config(cfg)  # Atomic persistence must succeed before activation signal.
                self.controls['status'].setText(
                    'Downloaded. Refresh desktop services in the extension and choose this model. '
                    'It loads only during capture and unloads when the last capture stops.'
                )
                self.show_runtime_placement(result, 'Test load: ')
                self.activated.emit(cfg)
            else:
                self.runtime.stop()
                message = 'Local runtime stopped before activation. Check runtime.log and retry.'
                self.controls['status'].setText(message)
                self.failed.emit(message)
        except Exception as exc:
            self.runtime.stop()
            self._pending_error = str(exc)
            self.controls['status'].setText(f'Could not enable local translation: {exc}')
            self.failed.emit(str(exc))
        finally:
            self.controls['progress'].setRange(0, 100)
            self.controls['progress'].setValue(100 if result is not None and not self._pending_error and not worker.cancel_event.is_set() else 0)
            self._set_busy(False)
            if result is not None and not self._pending_error and not worker.cancel_event.is_set():
                self._details()

    def cancel(self) -> None:
        if self.worker:
            self.controls['status'].setText('Cancelling...')
            self.worker.cancel()

    def stop(self, _checked=False) -> None:
        if self.busy:
            self.cancel()
            return
        try:
            cfg = deactivate_config(self.get_config())
            self.persist_config(cfg)
        except Exception as exc:
            self.controls['status'].setText(f'Could not save settings: {exc}')
            return
        self.runtime.stop()
        self._set_busy(False)
        self._details()
        self.controls['status'].setText('Removed from new capture choices. Files were kept; stop existing captures to unload the model.')
        self.deactivated.emit(cfg)

    def _check_runtime(self) -> None:
        if self.busy or self.closing or self.runtime.process is None or self.runtime.alive():
            return
        self.runtime.stop()
        self._set_busy(False)
        self.controls['status'].setText(f'Local runtime stopped unexpectedly. Check {self.root / "runtime.log"}; click Download and enable to retry.')
        self.failed.emit('Local translation runtime stopped unexpectedly.')

    def shutdown(self) -> bool:
        self.closing = True
        self.watchdog.stop()
        if self.catalog_worker and not self.catalog_worker.wait(100):
            return False
        if self.worker:
            self.worker.cancel()
        self.runtime.stop()
        return self.worker is None or self.worker.wait(8000)

    def collect_into_config(self, config: dict) -> dict:
        cfg = copy.deepcopy(config)
        local = cfg.setdefault('local_llama', {})
        if local.get('enabled') and local.get('model_id'):
            installed = local.setdefault('installed_models', [])
            if local['model_id'] not in installed:
                installed.append(local['model_id'])
            local['enabled'] = False
            _pin_legacy_local_default(cfg)
            if cfg.get('translation', {}).get('engine') == 'managed_llama':
                cfg['translation']['engine'] = 'noop'
                cfg['translation'].pop('managed_llama', None)
        local.update(self._runtime_settings())
        local.update(root=str(self.root), model_id=self.controls['model'].currentData())
        return cfg

    def _refresh_models(self, selected=None):
        combo = self.controls['model']
        selected = selected or combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for model in list_models(self.get_config()):
            combo.addItem(model.name, userData=model.id)
        combo.setCurrentIndex(max(0, combo.findData(selected)))
        combo.blockSignals(False)
        self._details()

    def add_model(self):
        if self.busy or self.catalog_worker:
            return
        dialog = AddModelDialog(self.controls['model'].window())
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self.catalog_worker = CatalogWorker(dialog.repo.text().strip(), dialog.filename.text().strip(), self)
        self.catalog_worker.outcome.connect(self._catalog_result)
        self.catalog_worker.finished.connect(self._catalog_finished)
        self._set_busy(True)
        self.controls['cancel'].setEnabled(False)
        self.controls['status'].setText('Reading model file metadata…')
        self.catalog_worker.start()

    def _catalog_result(self, model, error):
        if self.closing:
            return
        if error:
            self.controls['status'].setText(error)
            return
        try:
            cfg = self.get_config()
            custom = cfg.setdefault('local_llama', {}).setdefault('custom_models', [])
            if model.id not in {m.id for m in list_models(cfg)}:
                custom.append(asdict(model))
            self.persist_config(cfg)
            self._refresh_models(model.id)
            self.controls['status'].setText('Added. Review the source and size, then click Download model.')
        except Exception as exc:
            self.controls['status'].setText(str(exc))

    def _catalog_finished(self):
        worker, self.catalog_worker = self.catalog_worker, None
        worker.deleteLater()
        self._set_busy(False)


class CatalogWorker(QtCore.QThread):
    outcome = QtCore.Signal(object, str)

    def __init__(self, repo, filename, parent):
        super().__init__(parent)
        self.repo, self.filename = repo, filename

    def run(self):
        try:
            self.outcome.emit(resolve_hf_model(self.repo, self.filename), '')
        except Exception as exc:
            self.outcome.emit(None, str(exc))
