"""Lifecycle wrapper for the local STT backend.

The runner deliberately treats process creation and service readiness as two
different states. A child process that has been spawned can still fail while
importing dependencies, bind the wrong listener, or answer for another
instance. The GUI only receives ``started`` after the instance-specific health
contract has been observed.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from PySide6 import QtCore


_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|authorization)"
    r"(\s*[=:]\s*|\s+)([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")


def sanitize_log_line(line: str) -> str:
    """Remove common credentials before exposing child output to the GUI."""

    cleaned = _BEARER_RE.sub("Bearer <redacted>", str(line).rstrip())
    return _SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", cleaned)


class BackendRunner(QtCore.QThread):
    """Start, verify, and stop one owned backend process.

    ``started`` is intentionally emitted after readiness, preserving the old
    signal name for callers while fixing its meaning. Tests and embedders can
    inject ``process_factory`` and ``health_probe`` without launching the real
    backend.
    """

    status = QtCore.Signal(str)
    started = QtCore.Signal()
    ready = QtCore.Signal(object)
    stopped = QtCore.Signal()
    error = QtCore.Signal(str)
    log = QtCore.Signal(str)

    def __init__(
        self,
        config_path: str,
        host: str,
        port: int,
        *,
        ready_timeout: float = 30.0,
        health_interval: float = 0.25,
        process_factory: Optional[Callable[..., subprocess.Popen]] = None,
        health_probe: Optional[Callable[[str, str], Optional[Mapping[str, Any]]]] = None,
        project_root: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self.config_path = str(config_path)
        self.host = str(host)
        self.port = int(port)
        self.ready_timeout = max(0.1, float(ready_timeout))
        self.health_interval = max(0.02, float(health_interval))
        self._process_factory = process_factory or subprocess.Popen
        self._health_probe = health_probe or self._default_health_probe
        self._process: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()
        self._instance_id = ""
        self._project_root = project_root or Path(__file__).resolve().parents[1]
        self._recent_logs: queue.Queue[str] = queue.Queue(maxsize=200)
        self.health_payload: Optional[Mapping[str, Any]] = None

    @property
    def process(self) -> Optional[subprocess.Popen]:
        """The owned child handle, useful for diagnostics and tests."""

        return self._process

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def recent_logs(self) -> list[str]:
        return list(self._recent_logs.queue)

    def _emit_log(self, line: str) -> None:
        cleaned = sanitize_log_line(line)
        if not cleaned:
            return
        try:
            self._recent_logs.put_nowait(cleaned)
        except queue.Full:
            try:
                self._recent_logs.get_nowait()
                self._recent_logs.put_nowait(cleaned)
            except queue.Empty:
                pass
        self.log.emit(cleaned)

    def _read_process_output(self, process: subprocess.Popen) -> None:
        stream = getattr(process, "stdout", None)
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                if line == "":
                    break
                self._emit_log(line)
        except (OSError, ValueError):
            # The stream can be closed while stop() is reaping the child.
            return

    @staticmethod
    def _default_health_probe(url: str, expected_instance_id: str) -> Optional[Mapping[str, Any]]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=0.8) as response:
                raw = response.read()
        except (OSError, urllib.error.URLError, TimeoutError):
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, Mapping):
            return None
        return value

    def _is_ready(self, payload: Optional[Mapping[str, Any]]) -> bool:
        if not isinstance(payload, Mapping):
            return False
        return (
            payload.get("service") == "stt-tts"
            and payload.get("instance_id") == self._instance_id
            and payload.get("ready") is True
        )

    def _terminate_process(self, *, wait: bool = True) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            return
        if not wait:
            return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                return
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

    def _failure_message(self, prefix: str, exit_code: Optional[int] = None) -> str:
        suffix = f" (exit code {exit_code})" if exit_code is not None else ""
        recent = self.recent_logs[-8:]
        if recent:
            return f"{prefix}{suffix}: " + " | ".join(recent)
        return f"{prefix}{suffix}"

    def run(self) -> None:
        process: Optional[subprocess.Popen] = None
        ready_emitted = False
        try:
            self.health_payload = None
            # A runner represents exactly one launch attempt. Do not inherit a
            # previous process's ID from the parent environment; that would
            # allow a foreign listener to satisfy this instance's health check.
            self._instance_id = uuid.uuid4().hex
            if self._stop_event.is_set():
                return
            env = os.environ.copy()
            env["STT_CONFIG_PATH"] = self.config_path
            env["STT_INSTANCE_ID"] = self._instance_id
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--backend"]
            else:
                cmd = [sys.executable, "-m", "backend.server"]
            cmd.extend(
                [
                    "--host",
                    self.host,
                    "--port",
                    str(self.port),
                    "--config",
                    self.config_path,
                ]
            )
            creationflags = 0
            if sys.platform.startswith("win"):
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = self._process_factory(
                cmd,
                cwd=str(self._project_root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
            self._process = process
            reader = threading.Thread(
                target=self._read_process_output,
                args=(process,),
                name="stt-backend-log-reader",
                daemon=True,
            )
            reader.start()
            self.status.emit("Starting")
            deadline = time.monotonic() + self.ready_timeout
            health_host = self.host
            if health_host == "0.0.0.0":
                health_host = "127.0.0.1"
            elif health_host in {"::", "[::]"}:
                health_host = "[::1]"
            elif ":" in health_host and not health_host.startswith("["):
                health_host = f"[{health_host}]"
            health_url = f"http://{health_host}:{self.port}/health"
            while not self._stop_event.is_set():
                exit_code = process.poll()
                if exit_code is not None:
                    if not ready_emitted:
                        self.error.emit(
                            self._failure_message("Backend exited before ready", exit_code)
                        )
                    break
                payload = self._health_probe(health_url, self._instance_id)
                # A stop request or child exit can happen while the HTTP
                # probe is in flight. Never turn that stale response into a
                # Ready event.
                if self._stop_event.is_set() or process.poll() is not None:
                    break
                if self._is_ready(payload):
                    self.health_payload = payload
                    ready_emitted = True
                    self.status.emit("Ready")
                    self.ready.emit(payload)
                    self.started.emit()
                    break
                if time.monotonic() >= deadline:
                    self.error.emit(
                        self._failure_message(
                            f"Backend readiness timed out after {self.ready_timeout:.1f}s"
                        )
                    )
                    break
                self._stop_event.wait(self.health_interval)

            if self._stop_event.is_set() or not ready_emitted:
                self._terminate_process(wait=True)
            else:
                while not self._stop_event.is_set():
                    exit_code = process.poll()
                    if exit_code is not None:
                        if exit_code != 0:
                            self.error.emit(
                                self._failure_message("Backend stopped unexpectedly", exit_code)
                            )
                        break
                    self._stop_event.wait(0.1)
                if self._stop_event.is_set():
                    self._terminate_process(wait=True)
        except Exception as exc:
            self.error.emit(self._failure_message(f"{type(exc).__name__}: {exc}"))
            if process is not None:
                self._terminate_process(wait=True)
        finally:
            self._process = None
            self.status.emit("Stopped")
            self.stopped.emit()

    def stop(self) -> None:
        """Request shutdown without waiting on the GUI thread."""

        self._stop_event.set()
        # terminate() itself is non-blocking on supported platforms. All
        # bounded waits and force-kill handling remain in run().
        self._terminate_process(wait=False)
