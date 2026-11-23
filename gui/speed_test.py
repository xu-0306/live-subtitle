from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from PySide6 import QtCore, QtWidgets
from qfluentwidgets import BodyLabel, IndeterminateProgressBar, PlainTextEdit, PushButton, ScrollArea, SubtitleLabel

from backend.benchmark import verdict
from gui.backend_runner import sanitize_log_line
from gui.fluent_kit import FluentDialog
from gui.local_model_controller import runtime_placement_text


def speech_device_text(devices: dict) -> str:
    encoder, decoder = devices.get('encoder'), devices.get('decoder')
    if encoder == decoder == 'cuda':
        return 'Speech recognition: GPU (CUDA)'
    if encoder == decoder == 'cpu':
        return 'Speech recognition: CPU'
    labels = {'cuda': 'GPU', 'cpu': 'CPU'}
    if encoder in labels and decoder in labels:
        return f'Speech recognition: encoder {labels[encoder]}, decoder {labels[decoder]}'
    return ''


def last_test_date(config_path: Path) -> str:
    try:
        result = json.loads(config_path.with_name('benchmark.json').read_text(encoding='utf-8'))
        return datetime.fromisoformat(result['timestamp']).astimezone().date().isoformat()
    except (OSError, ValueError, TypeError, KeyError):
        return ''


def _delay(stats: dict) -> str:
    if stats.get('p50_ms') is None or stats.get('p95_ms') is None:
        return 'no delay measurements'
    return f'typical delay {stats["p50_ms"]:.0f} ms, worst {stats["p95_ms"]:.0f} ms'


class SpeedTestDialog(FluentDialog):
    def __init__(self, config_path: Path, apply_changes, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.widget.setFixedWidth(550)
        self._apply_changes = apply_changes
        self._result = None
        self._stdout = b''
        self._stderr = b''
        self._closed = False
        self._started = False
        self.viewLayout.addWidget(SubtitleLabel('Test speed'))
        self.status = self._label('Preparing the speed test')
        self.busy = IndeterminateProgressBar(self.widget)
        self.viewLayout.addWidget(self.busy)
        self.close_button = self.set_buttons([('Cancel', False, self.reject)])['Cancel']

        self.process = QtCore.QProcess(self)
        self.process.setProgram(sys.executable)
        entry = ['--benchmark'] if getattr(sys, 'frozen', False) else ['-m', 'backend.benchmark']
        self.process.setArguments([*entry, '--config', str(config_path.resolve())])
        self.process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)

    def start(self) -> None:
        if not self._closed and not self._started:
            self._started = True
            self.process.start()

    def done(self, result: int) -> None:
        self._closed = True
        if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(3000)
        super().done(result)

    def _label(self, text: str, layout=None) -> BodyLabel:
        label = BodyLabel(text)
        label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        (layout if layout is not None else self.viewLayout).addWidget(label)
        return label

    def _read_stdout(self) -> None:
        self._stdout += bytes(self.process.readAllStandardOutput())
        lines = self._stdout.split(b'\n')
        self._stdout = lines.pop()
        for line in lines:
            prefix, _, data = line.partition(b' ')
            if prefix not in {b'PROGRESS', b'RESULT'}:
                continue
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if prefix == b'PROGRESS':
                self.status.setText(str(payload.get('label') or 'Testing speed'))
            elif payload.get('version') == 1:
                self._result = payload

    def _read_stderr(self) -> None:
        self._stderr = (self._stderr + bytes(self.process.readAllStandardError()))[-12000:]

    def _process_error(self, error: QtCore.QProcess.ProcessError) -> None:
        if not self._closed and error == QtCore.QProcess.ProcessError.FailedToStart:
            self._show_result({}, f'Could not start the speed test: {self.process.errorString()}')

    def _finished(self, exit_code: int, exit_status: QtCore.QProcess.ExitStatus) -> None:
        if self._closed:
            return
        self._read_stdout()
        self._read_stderr()
        result = self._result or {}
        error = result.get('error')
        if not error and (exit_code or exit_status != QtCore.QProcess.ExitStatus.NormalExit or not result):
            error = f'The speed test ended without a usable result (exit code {exit_code}).'
        self._show_result(result, error)

    def _show_result(self, result: dict, error: str | None) -> None:
        self.busy.hide()
        self.status.setText(sanitize_log_line(error) if error else 'Speed test complete')
        self.close_button.setText('Close')
        details = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(details)
        layout.setContentsMargins(0, 0, 8, 0)
        if result:
            speech = result.get('stt') or {}
            translation = result.get('translation') or {}
            together = result.get('together') or {}
            self._label(f'Speech: {verdict(speech).capitalize()} · {_delay(speech)}'
                        if speech else 'Speech: Not measured', layout)
            if translation.get('status'):
                self._label('Translation: Skipped (not a local model)', layout)
            else:
                self._label(f'Translation: {verdict(translation).capitalize()} · {_delay(translation)}'
                            if translation else 'Translation: Not measured', layout)
            if together:
                shared_speech, shared_translation = together['stt'], together['translation']
                status = verdict(together).capitalize()
                self._label(f'Together: {status}\nSpeech: {_delay(shared_speech)}\nTranslation: {_delay(shared_translation)}', layout)
            else:
                self._label('Together: Not measured', layout)
            speech_where = speech_device_text(result.get('stt_devices') or {})
            translation_where = (f'Translation: {runtime_placement_text(result.get("placement"))}'
                                 if result.get('gpu_layers') is not None else '')
            if speech_where or translation_where:
                self._label('\n'.join(filter(None, (speech_where, translation_where))), layout)

        tail = [sanitize_log_line(line) for line in self._stderr.decode('utf-8', 'replace').splitlines()[-12:]]
        if error and any(tail):
            diagnostics = PlainTextEdit()
            diagnostics.setReadOnly(True)
            diagnostics.setFixedHeight(82)
            diagnostics.setPlainText('\n'.join(tail))
            layout.addWidget(diagnostics)
        recommendations = result.get('recommendations') or []
        if recommendations:
            self.notice = BodyLabel()
            self.notice.setWordWrap(True)
            self.notice.hide()
            for item in recommendations:
                row = QtWidgets.QHBoxLayout()
                self._label(item['text'], row)
                row.setStretch(0, 1)
                if item['changes']:
                    button = PushButton('Apply')
                    button.clicked.connect(lambda _checked=False, changes=item['changes'], button=button:
                                           self._apply(button, changes))
                    row.addWidget(button, 0, QtCore.Qt.AlignmentFlag.AlignTop)
                layout.addLayout(row)
            layout.addWidget(self.notice)
        if layout.count():
            layout.setSpacing(12)
            layout.addStretch(1)
            details.setStyleSheet('background: transparent')
            scroll = ScrollArea()
            scroll.enableTransparentBackground()
            scroll.setWidget(details)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            # Content height up to a cap; longer results scroll.
            scroll.setFixedHeight(min(details.sizeHint().height() + 8, 380))
            self.viewLayout.addWidget(scroll)

    def _apply(self, button: PushButton, changes: dict) -> None:
        saved = self._apply_changes(changes)
        self.notice.setText('Settings saved. Download any missing model on its model page.'
                            if saved else 'Settings could not be saved.')
        self.notice.show()
        if saved:
            button.setEnabled(False)
