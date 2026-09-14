"""Main-owned Qt lifecycle controller for the passive local model form."""
from __future__ import annotations

import copy
import threading
from dataclasses import asdict
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from backend.local_catalog import MODELS, RUNTIMES, get_model, list_models, resolve_hf_model
from backend.local_runtime import Cancelled, ManagedRuntime, detect_hardware, preflight, prepare_install
from gui import config_manager
from gui._local_model_form import build_form


def activate_config(config: dict, model_id: str, settings: dict, result: dict, target: str) -> dict:
    updated = copy.deepcopy(config)
    local = updated.setdefault('local_llama', {})
    # Installation publishes availability only; the extension chooses inference.
    installed = list(local.get('installed_models', []))
    if local.get('enabled') and local.get('model_id') not in installed:
        installed.append(local['model_id'])
    if model_id not in installed:
        installed.append(model_id)
    local.update(settings)
    local.update(enabled=False, model_id=model_id, target_language=target, installed_models=installed)
    translation = updated.setdefault('translation', {})
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
    if updated.get('translation', {}).get('engine') == 'managed_llama':
        updated['translation']['engine'] = 'noop'
    return updated


class InstallWorker(QtCore.QThread):
    progress = QtCore.Signal(str, object, object)
    hardware = QtCore.Signal(str)
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
            kind = 'windows-cpu' if self.settings['gpu_layers'] == 0 else 'windows-cuda'
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
        self.controls['gpu_layers'].setToolTip('Auto uses GPU offload after a shared-memory preflight. Set 0 for CPU translation.')
        self.runtime = ManagedRuntime()
        self.worker: InstallWorker | None = None
        self.closing = False
        self._pending_result = None
        self._pending_error = ''
        self._target = 'zh-TW'
        self.controls['hardware'].setText('Downloads do not load the GPU. Captures check the shared Whisper memory budget. Below 8 GB: use CPU or an external service.')
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
        for key, default in [('context', 2048), ('gpu_layers', -1), ('port', 18080)]:
            try:
                self.controls[key].setValue(int(local.get(key, default)))
            except (ValueError, TypeError):
                self.controls[key].setValue(default)
        self._refresh_models(local.get('model_id', MODELS[0].id))
        for key, value in [('model', local.get('model_id', MODELS[0].id)),
                           ('target', local.get('target_language', cfg.get('translation', {}).get('target_language', 'zh-TW')))]:
            index = self.controls[key].findData(value)
            if index >= 0:
                self.controls[key].setCurrentIndex(index)
            elif key == 'target':
                self.controls[key].setEditText(str(value))
        self._set_busy(False)

    def set_root(self, root: Path) -> bool:
        """Point future downloads at the shared model-storage location."""

        if self.busy or self.runtime.alive():
            return False
        self.root = Path(root)
        return True

    @property
    def busy(self) -> bool:
        return self.worker is not None

    def _details(self, *_args) -> None:
        model = get_model(self.controls['model'].currentData(), self.get_config())
        installed = self.get_config().get('local_llama', {}).get('installed_models', [])
        self.controls['stop'].setEnabled(not self.busy and model.id in installed)
        kind = 'windows-cpu' if self.controls['gpu_layers'].value() == 0 else 'windows-cuda'
        runtime_size = sum(a.size for a in RUNTIMES[kind])
        self.controls['details'].setText(
            'ON-DEVICE · MANAGED RUNTIME\n'
            f'Model: {model.size / 1024**3:.2f} GiB · execution components: {runtime_size / 1024**2:.0f} MiB on first use. '
            f'{model.license}. Runs locally; model files come from {model.repo}. '
            'Quality depends on language and content. Download includes all required GGUF parts. ' + model.notes
        )

    def _set_busy(self, busy: bool) -> None:
        for key in ('model', 'target', 'advanced', 'install', 'add_model'):
            self.controls[key].setEnabled(not busy and not self.runtime.alive())
        self.controls['cancel'].setEnabled(busy)
        self.controls['stop'].setEnabled(not busy and self.controls['model'].currentData() in self.get_config().get('local_llama', {}).get('installed_models', []))

    def install(self, _checked=False, *, allow_download=True) -> None:
        if self.busy or self.runtime.alive() or self.closing:
            return
        self._pending_result, self._pending_error = None, ''
        settings = {key: self.controls[key].value() for key in ('context', 'gpu_layers', 'port')}
        settings['root'] = str(self.root)
        self._target = self.target_language()
        worker = InstallWorker(self.root, self.controls['model'].currentData(), settings,
                               self.get_config().get('stt', {}), self.runtime, allow_download, self.get_config())
        self.worker = worker
        worker.progress.connect(self._progress)
        worker.hardware.connect(self.controls['hardware'].setText)
        worker.outcome.connect(self._outcome)
        worker.finished.connect(self._finished)
        self._set_busy(True)
        self._progress('Preparing local translation', 0, 0)
        worker.start()

    def _progress(self, message: str, done: int, total: int) -> None:
        bar = self.controls['progress']
        bar.setRange(0, 100 if total else 0)
        if total:
            bar.setValue(min(100, int(done * 100 / total)))
        self.controls['status'].setText(message)

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
                cfg = activate_config(self.get_config(), worker.model_id, worker.settings, result, self._target)
                self.persist_config(cfg)  # Atomic persistence must succeed before activation signal.
                self.controls['status'].setText(
                    'Downloaded. Refresh desktop services in the extension and choose this model. '
                    'It loads only during capture and unloads when the last capture stops.'
                )
                self.activated.emit(cfg)
            else:
                self.runtime.stop()
                message = 'Local runtime stopped before activation. Check runtime.log and retry.'
                self.controls['status'].setText(message)
                self.failed.emit(message)
        except Exception as exc:
            self.runtime.stop()
            self.controls['status'].setText(f'Could not enable local translation: {exc}')
            self.failed.emit(str(exc))
        finally:
            self.controls['progress'].setRange(0, 100)
            self.controls['progress'].setValue(100 if result is not None and not self._pending_error and not worker.cancel_event.is_set() else 0)
            self._set_busy(False)

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

    def target_language(self) -> str:
        combo = self.controls['target']
        index = combo.currentIndex()
        if index >= 0 and combo.currentText() == combo.itemText(index):
            return str(combo.itemData(index))
        return combo.currentText().strip() or 'zh-TW'

    def collect_into_config(self, config: dict) -> dict:
        cfg = copy.deepcopy(config)
        local = cfg.setdefault('local_llama', {})
        if local.get('enabled') and local.get('model_id'):
            installed = local.setdefault('installed_models', [])
            if local['model_id'] not in installed:
                installed.append(local['model_id'])
            local['enabled'] = False
            if cfg.get('translation', {}).get('engine') == 'managed_llama':
                cfg['translation']['engine'] = 'noop'
                cfg['translation'].pop('managed_llama', None)
        local.update({key: self.controls[key].value() for key in ('context', 'gpu_layers', 'port')})
        local.update(root=str(self.root), target_language=self.target_language(),
                     model_id=self.controls['model'].currentData())
        cfg.setdefault('translation', {})['target_language'] = self.target_language()
        return cfg

    def _refresh_models(self, selected=None):
        combo = self.controls['model']
        selected = selected or combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for model in list_models(self.get_config()):
            combo.addItem(model.name, model.id)
        combo.setCurrentIndex(max(0, combo.findData(selected)))
        combo.blockSignals(False)
        self._details()

    def add_model(self):
        if self.busy or self.catalog_worker:
            return
        dialog = QtWidgets.QDialog(self.controls['model'].window())
        dialog.setWindowTitle('Add public Hugging Face GGUF')
        form = QtWidgets.QFormLayout(dialog)
        repo, filename = QtWidgets.QLineEdit(), QtWidgets.QLineEdit()
        repo.setPlaceholderText('owner/repository')
        filename.setPlaceholderText('model-Q4_K_M.gguf (or first shard path)')
        form.addRow('Repository', repo)
        form.addRow('GGUF file', filename)
        notice = QtWidgets.QLabel('Public GGUF weights only. The model must support a standard chat template. '
                                 'A pinned revision and SHA256 will be saved; no repository code runs.')
        notice.setWordWrap(True)
        form.addRow(notice)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self.catalog_worker = CatalogWorker(repo.text().strip(), filename.text().strip(), self)
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
