from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from gui.backend_runner import BackendRunner, sanitize_log_line


def _app() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance()
    return app or QtWidgets.QApplication([])


class _Stream:
    def readline(self) -> str:
        return ""


class _Process:
    def __init__(self) -> None:
        self.returncode = None
        self.stdout = _Stream()
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_runner_emits_started_only_after_matching_health_and_passes_instance_env() -> None:
    _app()
    process = _Process()
    calls: list[tuple[str, str]] = []
    holder: dict[str, BackendRunner] = {}

    def factory(*args, **kwargs):
        calls.append((kwargs["env"]["STT_INSTANCE_ID"], kwargs["env"]["STT_CONFIG_PATH"]))
        return process

    probes: list[tuple[str, str]] = []

    def probe(url: str, instance_id: str):
        probes.append((url, instance_id))
        if len(probes) == 1:
            return {"service": "stt-tts", "instance_id": "another", "ready": True}
        return {"service": "stt-tts", "instance_id": instance_id, "ready": True}

    runner = BackendRunner(
        "isolated.yaml",
        "0.0.0.0",
        8765,
        ready_timeout=1,
        health_interval=0.01,
        process_factory=factory,
        health_probe=probe,
    )
    holder["runner"] = runner
    statuses: list[str] = []
    runner.status.connect(statuses.append)
    runner.start()
    # Stop after the successful readiness transition.  A stop requested while
    # the probe is still in flight must never be reported as Ready (covered by
    # the dedicated race test below).
    timer = threading.Timer(0.08, runner.stop)
    timer.start()
    assert runner.wait(2000)
    timer.cancel()
    _app().processEvents()

    assert calls and calls[0][0] == runner.instance_id
    assert calls[0][1] == "isolated.yaml"
    assert probes[0][0] == "http://127.0.0.1:8765/health"
    assert runner.health_payload == {
        "service": "stt-tts",
        "instance_id": runner.instance_id,
        "ready": True,
    }
    assert "Ready" in statuses
    assert process.terminated


def test_stop_during_health_probe_never_emits_ready() -> None:
    _app()
    process = _Process()
    holder: dict[str, BackendRunner] = {}
    probe_started = threading.Event()

    def probe(_url: str, instance_id: str):
        probe_started.set()
        holder["runner"].stop()
        return {"service": "stt-tts", "instance_id": instance_id, "ready": True}

    runner = BackendRunner(
        "isolated.yaml",
        "127.0.0.1",
        8765,
        ready_timeout=1,
        health_interval=0.01,
        process_factory=lambda *args, **kwargs: process,
        health_probe=probe,
    )
    holder["runner"] = runner
    started: list[dict] = []
    runner.started.connect(started.append)
    runner.start()
    assert runner.wait(2000)
    _app().processEvents()

    assert probe_started.is_set()
    assert started == []
    assert process.terminated


def test_runner_identity_mismatch_times_out_and_reaps_child() -> None:
    _app()
    process = _Process()
    errors: list[str] = []
    runner = BackendRunner(
        "isolated.yaml",
        "127.0.0.1",
        8765,
        ready_timeout=0.08,
        health_interval=0.01,
        process_factory=lambda *args, **kwargs: process,
        health_probe=lambda _url, _instance: {
            "service": "stt-tts",
            "instance_id": "foreign",
            "ready": True,
        },
    )
    runner.error.connect(errors.append)
    runner.start()
    assert runner.wait(2000)
    _app().processEvents()
    assert errors and "timed out" in errors[0].lower()
    assert process.terminated


def test_stop_before_thread_spawn_does_not_launch_child() -> None:
    _app()
    launched = []
    runner = BackendRunner(
        "isolated.yaml",
        "127.0.0.1",
        8765,
        process_factory=lambda *args, **kwargs: launched.append(True),
    )
    runner.stop()
    runner.start()
    assert runner.wait(2000)
    assert launched == []


def test_log_sanitization_removes_credentials_and_bearer_tokens() -> None:
    cleaned = sanitize_log_line("api_key=secret Bearer abc123 password:pass")
    assert "secret" not in cleaned
    assert "abc123" not in cleaned
    assert "password=pass" not in cleaned
    assert "<redacted>" in cleaned
