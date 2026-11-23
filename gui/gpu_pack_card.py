"""Speech recognition settings for the downloadable CUDA torch wheel."""
from __future__ import annotations

import copy
import os
import sys
from threading import Event

from PySide6 import QtCore, QtWidgets
from qfluentwidgets import BodyLabel, ProgressBar, PushButton, SimpleCardWidget, StrongBodyLabel

from backend import gpu_pack
from backend.local_runtime import Cancelled
from gui import fluent_kit


class _Worker(QtCore.QThread):
    progress = QtCore.Signal(str, object, object)

    def __init__(self, job, parent):
        super().__init__(parent)
        self.job = job
        self.cancel = Event()
        self.result = None
        self.error = None

    def run(self) -> None:
        try:
            self.result = self.job(self.cancel, self.progress.emit)
        except Exception as exc:
            self.error = exc


class GpuPackCard(SimpleCardWidget):
    def __init__(self, *, get_config, save_config, get_runner, stop_backend, start_backend, notice):
        super().__init__()
        self._get_config = get_config
        self._save_config = save_config
        self._get_runner = get_runner
        self._stop_backend = stop_backend
        self._start_backend = start_backend
        self._notice = notice
        self._worker = None
        self._operation = ''
        self._gpu = None
        self._closing = False
        self.removing = False
        self._restart_after_remove = False
        self._detect_on_show = bool(getattr(sys, 'frozen', False) or 'LIVE_SUBTITLE_GPU_PACK_FORCE' in os.environ)

        self.setObjectName('card')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        title = StrongBodyLabel('GPU speech pack')
        title.setObjectName('cardTitle')
        layout.addWidget(title)

        self.status = BodyLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.progress = ProgressBar()
        layout.addWidget(self.progress)
        actions = QtWidgets.QHBoxLayout()
        self.install_button = PushButton('Install')
        self.cancel_button = PushButton('Cancel')
        self.check_button = PushButton('Check again')
        self.remove_button = PushButton('Remove')
        self._buttons = (self.install_button, self.cancel_button, self.check_button, self.remove_button)
        for button in self._buttons:
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.install_button.clicked.connect(self._install)
        self.cancel_button.clicked.connect(self._cancel)
        self.check_button.clicked.connect(self._verify)
        self.remove_button.clicked.connect(self._request_remove)
        self._show_status('Checking for an NVIDIA GPU...' if self._detect_on_show else
                          'Not needed when running from source.')

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._detect_on_show:
            self._detect_on_show = False
            self._run('detect', lambda _cancel, _progress: gpu_pack.detect_nvidia())

    def _show_status(self, text, *buttons) -> None:
        self.status.setText(text)
        self.progress.setVisible(self.cancel_button in buttons)
        for button in self._buttons:
            button.setVisible(button in buttons)

    def _refresh(self) -> None:
        tag, reason = gpu_pack.choose_pack(self._gpu)
        if tag is None:
            self._show_status(reason)
            return
        pack = gpu_pack.installed()
        if pack is None:
            size = gpu_pack.PACKS[tag].size / 1_000_000_000
            self._show_status(
                f"Runs speech recognition on {self._gpu['name']} with CUDA {self._cuda(tag)}. "
                f'{size:.1f} GB download, about {1.6 * size:.1f} GB on disk. '  # measured: unpacked CUDA torch ~1.5x the wheel
                'Stored outside the app folder, so updates keep it.', self.install_button,
            )
        elif pack['verified']:
            self._show_status(f"Installed \u00b7 CUDA {self._cuda(pack['tag'])} \u00b7 used after the server restarts.",
                              self.remove_button)
        else:
            reason = pack.get('reason') or 'Unknown error'
            self._show_status(f'Installed, but CUDA did not start: {reason.rstrip(".")}. '
                              'Speech recognition stays on the CPU.', self.check_button, self.remove_button)

    @staticmethod
    def _cuda(tag: str) -> str:
        return f'{tag[2:4]}.{tag[4:]}'

    def _run(self, operation, job) -> None:
        self._operation = operation
        self._worker = _Worker(job, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _install(self) -> None:
        tag, _reason = gpu_pack.choose_pack(self._gpu)
        if tag is None or self._worker is not None:
            return
        self.cancel_button.setEnabled(True)
        self._show_status('Preparing download...', self.cancel_button)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self._run('install', lambda cancel, progress: gpu_pack.install(tag, cancel, progress))

    def _cancel(self) -> None:
        if self._operation == 'install' and self._worker is not None:
            self._worker.cancel.set()
            self.cancel_button.setEnabled(False)
            self.status.setText('Cancelling...')

    def _on_progress(self, text: str, done: int, total: int) -> None:
        if not self.cancel_button.isEnabled():
            return
        if total:
            self.progress.setValue(done * 1000 // total)
            if text.startswith('Unpacking'):
                text = f'{text}: {done:,} / {total:,} files'
            else:
                text = f'Downloading GPU speech pack: {done / 1e9:.2f} / {total / 1e9:.2f} GB'
        self.status.setText(text)

    def _verify(self) -> None:
        if self._worker is not None:
            return
        self._show_status('Checking CUDA\u2026')
        command = ([sys.executable, '--gpu-pack-check'] if getattr(sys, 'frozen', False) else
                   [sys.executable, '-m', 'backend.gpu_pack', '--check'])
        self._run('verify', lambda _cancel, _progress: gpu_pack.verify(command))

    def _request_remove(self) -> None:
        if self._worker is not None or self.removing:
            return
        runner = self._get_runner()
        running = runner is not None and runner.isRunning()
        if running and fluent_kit.ask(
            self.window(), 'Remove GPU speech pack',
            'The server stops while the pack is removed and starts again afterwards.',
            QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel, QtWidgets.QMessageBox.Ok,
        ) != QtWidgets.QMessageBox.Ok:
            return
        if self._closing:
            return
        self.removing = True
        self._restart_after_remove = running
        self._show_status('Removing GPU speech pack\u2026')
        runner = self._get_runner()
        if runner is not None and runner.isRunning():
            runner.finished.connect(self._start_remove)
            self._stop_backend()
            if runner.isRunning():
                return
        self._start_remove()

    @QtCore.Slot()
    def _start_remove(self) -> None:
        if self.removing and self._worker is None:
            self._run('remove', lambda _cancel, _progress: gpu_pack.remove())

    def _save_policy(self, installed: bool) -> bool:
        config = copy.deepcopy(self._get_config())
        stt = config.setdefault('stt', {})
        if installed and stt.get('backend_policy', 'localagreement') == 'localagreement':
            stt.update(backend_policy='simulstreaming', policy_set_by_gpu_pack=True)
        elif not installed and stt.pop('policy_set_by_gpu_pack', False):
            stt['backend_policy'] = 'localagreement'
        else:
            return True
        return self._save_config(config)

    @QtCore.Slot()
    def _on_finished(self) -> None:
        worker, operation = self._worker, self._operation
        self._worker = None
        self._operation = ''
        worker.deleteLater()
        if worker.error is not None:
            if not isinstance(worker.error, Cancelled) and not self._closing:
                self._notice(f'GPU speech pack: {worker.error}', 'error')
        elif operation == 'detect':
            self._gpu = worker.result
        elif operation == 'install' and not self._closing:
            self._verify()
            return
        elif operation == 'verify' and worker.result['verified']:
            if self._save_policy(installed=True) and not self._closing:
                policy = self._get_config().get('stt', {}).get('backend_policy')
                message = 'GPU speech pack ready. Restart the server to use the GPU.'
                if policy == 'simulstreaming':
                    message = ('GPU speech pack ready. Speech recognition now uses SimulStreaming; '
                               'restart the server to use the GPU.')
                self._notice(message)
        elif operation == 'remove':
            self._save_policy(installed=False)

        self._refresh()
        if operation == 'remove':
            self.removing = False
            if self._restart_after_remove and not self._closing:
                self._start_backend()

    def shutdown(self) -> bool:
        self._closing = True
        self._cancel()
        return self._worker is None and not self.removing
