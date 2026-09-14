from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, Mapping

from PySide6 import QtCore, QtWidgets

from gui.vllm_profiles import (
    build_launch_command,
    default_profile,
    load_profiles,
    resolve_request_url,
    update_config,
)


class VllmSettingsWidget(QtWidgets.QWidget):
    """Profile editor for OpenAI-compatible vLLM translation services."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._profiles: list[Dict[str, Any]] = []
        self._active_index = -1
        self._loading = False
        self._dirty = False
        self._section_present = False
        self._build_ui()
        self._connect_signals()

    def _build_ui(self) -> None:
        root_layout = QtWidgets.QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 12)
        layout.setSpacing(12)

        header = QtWidgets.QFrame()
        header.setObjectName("vllmHeader")
        header_layout = QtWidgets.QVBoxLayout(header)
        eyebrow = QtWidgets.QLabel("TRANSLATION INFERENCE")
        eyebrow.setObjectName("eyebrow")
        title = QtWidgets.QLabel("Translation services")
        title.setObjectName("sectionTitle")
        subtitle = QtWidgets.QLabel(
            "Save cloud or self-hosted API connections here, then select a service in the browser extension. "
            "Saving a profile does not start translation."
        )
        subtitle.setObjectName("mutedText")
        subtitle.setWordWrap(True)
        header_layout.addWidget(eyebrow)
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        layout.addWidget(header)

        profile_group = QtWidgets.QGroupBox("Profile")
        profile_layout = QtWidgets.QVBoxLayout(profile_group)
        self.use_vllm_check = QtWidgets.QCheckBox(
            "Use active vLLM profile for translation"
        )
        self.use_vllm_check.setObjectName("vllmActivation")
        self.use_vllm_check.setToolTip(
            "When enabled, saving defaults sets translation.engine to vllm. "
            "When disabled, the previous translation engine is restored."
        )
        self.use_vllm_check.hide()  # Legacy compatibility; the extension selects the service.
        profile_row = QtWidgets.QHBoxLayout()
        self.profile_combo = QtWidgets.QComboBox()
        self.profile_combo.setMinimumWidth(240)
        self.new_profile_btn = QtWidgets.QPushButton("New")
        self.duplicate_profile_btn = QtWidgets.QPushButton("Duplicate")
        self.delete_profile_btn = QtWidgets.QPushButton("Delete")
        self.delete_profile_btn.setProperty("kind", "danger")
        profile_row.addWidget(self.profile_combo, 1)
        profile_row.addWidget(self.new_profile_btn)
        profile_row.addWidget(self.duplicate_profile_btn)
        profile_row.addWidget(self.delete_profile_btn)
        profile_layout.addLayout(profile_row)
        layout.addWidget(profile_group)

        connection_group = QtWidgets.QGroupBox("Connection")
        connection_form = QtWidgets.QFormLayout(connection_group)
        connection_form.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.AllNonFixedFieldsGrow
        )

        self.name_input = QtWidgets.QLineEdit()
        self.name_input.setPlaceholderText("e.g. Workstation 4090")

        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItem("vLLM launch helper (advanced)", "local")
        self.mode_combo.addItem("Cloud / self-hosted API", "external")

        self.endpoint_input = QtWidgets.QLineEdit()
        self.endpoint_input.setPlaceholderText("http://127.0.0.1:8000/v1")
        self.endpoint_input.setClearButtonEnabled(True)

        self.auto_complete_check = QtWidgets.QCheckBox(
            "Automatically complete API endpoint"
        )
        self.auto_complete_check.setChecked(True)
        self.auto_complete_check.setToolTip(
            "Disable this when the provider requires the exact URL as entered."
        )

        self.api_type_combo = QtWidgets.QComboBox()
        self.api_type_combo.addItem("Chat Completions", "chat_completions")
        self.api_type_combo.addItem("Responses", "responses")
        self.api_type_combo.addItem("Messages", "messages")

        self.resolved_url_input = QtWidgets.QLineEdit()
        self.resolved_url_input.setReadOnly(True)
        self.resolved_url_input.setPlaceholderText("Resolved request URL")

        self.api_key_input = QtWidgets.QLineEdit()
        self.api_key_input.setEchoMode(QtWidgets.QLineEdit.Password)
        self.api_key_input.setPlaceholderText("Optional")
        self.api_key_input.setClearButtonEnabled(True)
        self.api_key_input.setToolTip(
            "Stored in the user config when defaults are saved. It is never "
            "shown in logs or launch-command previews."
        )
        self.show_api_key_btn = QtWidgets.QToolButton()
        self.show_api_key_btn.setText("Show")
        self.show_api_key_btn.setCheckable(True)
        api_key_row = QtWidgets.QWidget()
        api_key_layout = QtWidgets.QHBoxLayout(api_key_row)
        api_key_layout.setContentsMargins(0, 0, 0, 0)
        api_key_layout.addWidget(self.api_key_input, 1)
        api_key_layout.addWidget(self.show_api_key_btn)
        self.api_key_note = QtWidgets.QLabel(
            "Masked in the UI; saved to the per-user config when defaults are saved."
        )
        self.api_key_note.setObjectName("mutedText")
        self.api_key_note.setWordWrap(True)

        self.served_model_input = QtWidgets.QLineEdit()
        self.served_model_input.setPlaceholderText("Model name exposed by the API")

        self.max_concurrency_input = QtWidgets.QSpinBox()
        self.max_concurrency_input.setRange(0, 4096)
        self.max_concurrency_input.setSpecialValueText("Backend default")
        self.max_concurrency_input.setToolTip(
            "Maximum simultaneous translation requests for this profile. This is "
            "a client-side scheduler limit, not the vLLM max-num-seqs setting."
        )

        self.local_model_input = QtWidgets.QLineEdit()
        self.local_model_input.setPlaceholderText("Hugging Face model ID or local path")

        connection_form.addRow("Profile name", self.name_input)
        connection_form.addRow("Service mode", self.mode_combo)
        connection_form.addRow("API URL", self.endpoint_input)
        connection_form.addRow("", self.auto_complete_check)
        connection_form.addRow("API type", self.api_type_combo)
        connection_form.addRow("Request URL", self.resolved_url_input)
        connection_form.addRow("API key", api_key_row)
        connection_form.addRow("", self.api_key_note)
        connection_form.addRow("Served model", self.served_model_input)
        connection_form.addRow("Client concurrency", self.max_concurrency_input)
        self.connection_form = connection_form
        connection_form.addRow("Local model / path", self.local_model_input)
        layout.addWidget(connection_group)

        self.runtime_group = QtWidgets.QGroupBox("Local runtime")
        self.runtime_toggle = QtWidgets.QToolButton()
        self.runtime_toggle.setText("▸ Advanced runtime settings")
        self.runtime_toggle.setCheckable(True)
        self.runtime_toggle.setChecked(False)
        self.runtime_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        layout.addWidget(self.runtime_toggle)
        runtime_layout = QtWidgets.QVBoxLayout(self.runtime_group)
        self.runtime_notice = QtWidgets.QLabel()
        self.runtime_notice.setObjectName("runtimeNotice")
        self.runtime_notice.setWordWrap(True)
        runtime_layout.addWidget(self.runtime_notice)

        runtime_form = QtWidgets.QFormLayout()
        runtime_form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldsStayAtSizeHint)
        self.max_model_len_input = QtWidgets.QSpinBox()
        self.max_model_len_input.setRange(1, 10_000_000)
        self.max_model_len_input.setSingleStep(1024)
        self.max_num_seqs_input = QtWidgets.QSpinBox()
        self.max_num_seqs_input.setRange(1, 4096)
        self.tensor_parallel_input = QtWidgets.QSpinBox()
        self.tensor_parallel_input.setRange(1, 64)
        self.gpu_memory_input = QtWidgets.QDoubleSpinBox()
        self.gpu_memory_input.setRange(0.05, 1.0)
        self.gpu_memory_input.setSingleStep(0.05)
        self.gpu_memory_input.setDecimals(2)
        self.dtype_combo = QtWidgets.QComboBox()
        for label, value in (
            ("Auto", "auto"),
            ("Float16", "float16"),
            ("BFloat16", "bfloat16"),
            ("Float32", "float32"),
        ):
            self.dtype_combo.addItem(label, value)
        self.quantization_combo = QtWidgets.QComboBox()
        for label, value in (
            ("Auto / none", "auto"),
            ("AWQ", "awq"),
            ("GPTQ", "gptq"),
            ("BitsAndBytes", "bitsandbytes"),
            ("FP8", "fp8"),
            ("Compressed tensors", "compressed-tensors"),
        ):
            self.quantization_combo.addItem(label, value)

        runtime_form.addRow("max-model-len", self.max_model_len_input)
        runtime_form.addRow("max-num-seqs", self.max_num_seqs_input)
        runtime_form.addRow("Tensor parallelism", self.tensor_parallel_input)
        runtime_form.addRow("GPU memory utilization", self.gpu_memory_input)
        runtime_form.addRow("dtype", self.dtype_combo)
        runtime_form.addRow("Quantization", self.quantization_combo)
        runtime_layout.addLayout(runtime_form)

        command_label = QtWidgets.QLabel("Launch command preview")
        command_label.setObjectName("mutedText")
        runtime_layout.addWidget(command_label)
        self.command_preview = QtWidgets.QPlainTextEdit()
        self.command_preview.setReadOnly(True)
        self.command_preview.setMaximumHeight(94)
        self.command_preview.setObjectName("commandPreview")
        runtime_layout.addWidget(self.command_preview)
        copy_row = QtWidgets.QHBoxLayout()
        copy_row.addStretch(1)
        self.copy_command_btn = QtWidgets.QPushButton("Copy command")
        copy_row.addWidget(self.copy_command_btn)
        runtime_layout.addLayout(copy_row)
        layout.addWidget(self.runtime_group)
        layout.addStretch(1)

        scroll.setWidget(content)
        root_layout.addWidget(scroll)

        self._runtime_fields: tuple[QtWidgets.QWidget, ...] = (
            self.local_model_input,
            self.max_model_len_input,
            self.max_num_seqs_input,
            self.tensor_parallel_input,
            self.gpu_memory_input,
            self.dtype_combo,
            self.quantization_combo,
            self.command_preview,
            self.copy_command_btn,
        )

    def _connect_signals(self) -> None:
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        self.new_profile_btn.clicked.connect(self._new_profile)
        self.duplicate_profile_btn.clicked.connect(self._duplicate_profile)
        self.delete_profile_btn.clicked.connect(self._delete_profile)
        self.show_api_key_btn.toggled.connect(self._toggle_api_key_visibility)
        self.copy_command_btn.clicked.connect(self._copy_command)
        self.use_vllm_check.toggled.connect(self._settings_changed)
        self.mode_combo.currentIndexChanged.connect(self._settings_changed)
        self.api_type_combo.currentIndexChanged.connect(self._settings_changed)
        self.auto_complete_check.toggled.connect(self._settings_changed)
        self.dtype_combo.currentIndexChanged.connect(self._settings_changed)
        self.quantization_combo.currentIndexChanged.connect(self._settings_changed)
        self.runtime_toggle.toggled.connect(self._toggle_runtime_advanced)
        for widget in (
            self.name_input,
            self.endpoint_input,
            self.api_key_input,
            self.served_model_input,
            self.local_model_input,
        ):
            widget.textChanged.connect(self._settings_changed)
        for widget in (
            self.max_model_len_input,
            self.max_num_seqs_input,
            self.tensor_parallel_input,
            self.gpu_memory_input,
            self.max_concurrency_input,
        ):
            widget.valueChanged.connect(self._settings_changed)

    def load_from_config(self, config: Mapping[str, Any]) -> None:
        profiles, active_profile, section_present = load_profiles(config)
        self._profiles = profiles
        self._section_present = section_present
        self._dirty = False
        translation = config.get("translation")
        translation_map = translation if isinstance(translation, Mapping) else {}
        self._loading = True
        self.use_vllm_check.setChecked(
            str(translation_map.get("engine") or "").strip().lower() == "vllm"
        )
        self._loading = False
        active_index = next(
            (
                index
                for index, profile in enumerate(profiles)
                if profile["id"] == active_profile
            ),
            0,
        )
        self._refresh_profile_combo(active_index)
        self._active_index = active_index
        self._load_profile(profiles[active_index])
        self._dirty = False

    def collect_into_config(self, config: Mapping[str, Any]) -> Dict[str, Any]:
        self._store_current_profile()
        if not self._section_present and not self._dirty:
            return copy.deepcopy(dict(config))
        active_id = (
            self._profiles[self._active_index]["id"]
            if 0 <= self._active_index < len(self._profiles)
            else ""
        )
        return update_config(
            config,
            self._profiles,
            active_id,
            activate=None,
        )

    def _refresh_profile_combo(self, selected_index: int) -> None:
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self._profiles:
            self.profile_combo.addItem(profile["name"], profile["id"])
        self.profile_combo.setCurrentIndex(selected_index)
        self.profile_combo.blockSignals(False)
        self.delete_profile_btn.setEnabled(len(self._profiles) > 1)

    def _on_profile_changed(self, index: int) -> None:
        if self._loading or index < 0 or index >= len(self._profiles):
            return
        self._store_current_profile()
        self._active_index = index
        self._load_profile(self._profiles[index])
        self._dirty = True

    def _new_profile(self) -> None:
        self._store_current_profile()
        profile = default_profile(uuid.uuid4().hex)
        profile["name"] = f"Translation service {len(self._profiles) + 1}"
        self._profiles.append(profile)
        self._active_index = len(self._profiles) - 1
        self._refresh_profile_combo(self._active_index)
        self._load_profile(profile)
        self._dirty = True

    def _duplicate_profile(self) -> None:
        if not (0 <= self._active_index < len(self._profiles)):
            return
        self._store_current_profile()
        profile = copy.deepcopy(self._profiles[self._active_index])
        profile["id"] = uuid.uuid4().hex
        profile["name"] = f'{profile["name"]} copy'
        self._profiles.append(profile)
        self._active_index = len(self._profiles) - 1
        self._refresh_profile_combo(self._active_index)
        self._load_profile(profile)
        self._dirty = True

    def _delete_profile(self) -> None:
        if len(self._profiles) <= 1 or not (
            0 <= self._active_index < len(self._profiles)
        ):
            return
        profile_name = self._profiles[self._active_index]["name"]
        reply = QtWidgets.QMessageBox.question(
            self,
            "Delete translation service",
            f'Delete "{profile_name}"?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        del self._profiles[self._active_index]
        self._active_index = min(self._active_index, len(self._profiles) - 1)
        self._refresh_profile_combo(self._active_index)
        self._load_profile(self._profiles[self._active_index])
        self._dirty = True

    def _load_profile(self, profile: Mapping[str, Any]) -> None:
        self._loading = True
        try:
            self.name_input.setText(str(profile.get("name") or ""))
            self._set_combo_value(self.mode_combo, str(profile.get("mode") or "external"))
            self.endpoint_input.setText(str(profile.get("base_url") or ""))
            self.api_key_input.setText(str(profile.get("api_key") or ""))
            self.served_model_input.setText(str(profile.get("served_model") or ""))
            self.local_model_input.setText(str(profile.get("model") or ""))
            self.max_concurrency_input.setValue(
                int(profile.get("max_concurrency") or 0)
            )
            self._set_combo_value(
                self.api_type_combo,
                str(profile.get("api_type") or "chat_completions"),
            )
            self.auto_complete_check.setChecked(
                bool(profile.get("auto_complete_endpoint", True))
            )
            runtime = profile.get("runtime") or {}
            self.max_model_len_input.setValue(int(runtime.get("max_model_len", 4096)))
            self.max_num_seqs_input.setValue(int(runtime.get("max_num_seqs", 16)))
            self.tensor_parallel_input.setValue(
                int(runtime.get("tensor_parallel_size", 1))
            )
            self.gpu_memory_input.setValue(
                float(runtime.get("gpu_memory_utilization", 0.9))
            )
            self._set_combo_value(
                self.dtype_combo, str(runtime.get("dtype") or "auto"), allow_custom=True
            )
            self._set_combo_value(
                self.quantization_combo,
                str(runtime.get("quantization") or "auto"),
                allow_custom=True,
            )
        finally:
            self._loading = False
        self._refresh_derived_fields()

    def _store_current_profile(self) -> None:
        if not (0 <= self._active_index < len(self._profiles)):
            return
        profile = copy.deepcopy(self._profiles[self._active_index])
        runtime_source = profile.get("runtime")
        runtime = (
            copy.deepcopy(runtime_source)
            if isinstance(runtime_source, dict)
            else {}
        )
        runtime.update(
            {
                "max_model_len": self.max_model_len_input.value(),
                "max_num_seqs": self.max_num_seqs_input.value(),
                "tensor_parallel_size": self.tensor_parallel_input.value(),
                "gpu_memory_utilization": self.gpu_memory_input.value(),
                "dtype": self.dtype_combo.currentData() or "auto",
                "quantization": self.quantization_combo.currentData() or "auto",
            }
        )
        profile.update(
            {
                "name": self.name_input.text().strip()
                or f"Translation service {self._active_index + 1}",
                "mode": self.mode_combo.currentData() or "external",
                "base_url": self.endpoint_input.text().strip(),
                "api_key": self.api_key_input.text(),
                "served_model": self.served_model_input.text().strip(),
                "api_type": self.api_type_combo.currentData()
                or "chat_completions",
                "auto_complete_endpoint": self.auto_complete_check.isChecked(),
                "model": self.local_model_input.text().strip(),
                "max_concurrency": self.max_concurrency_input.value(),
                "runtime": runtime,
            }
        )
        self._profiles[self._active_index] = profile

    def _settings_changed(self, *_args: object) -> None:
        if self._loading:
            return
        self._dirty = True
        self._refresh_derived_fields()
        if 0 <= self._active_index < self.profile_combo.count():
            label = self.name_input.text().strip() or f"Translation service {self._active_index + 1}"
            self.profile_combo.setItemText(self._active_index, label)

    def _refresh_derived_fields(self) -> None:
        mode = self.mode_combo.currentData() or "external"
        is_local = mode == "local"
        self.runtime_group.setVisible(is_local and self.runtime_toggle.isChecked())
        self.runtime_toggle.setEnabled(is_local)
        self.local_model_input.setVisible(is_local)
        self.connection_form.labelForField(self.local_model_input).setVisible(is_local)
        for field in self._runtime_fields:
            field.setEnabled(is_local)
        if is_local:
            self.runtime_notice.setText(
                "These values are saved for a local vLLM launch command. "
                "Installing and starting vLLM remains a separate operation."
            )
        else:
            self.runtime_notice.setText(
                "External services are already running: OpenAI-compatible APIs "
                "cannot apply max-model-len, max-num-seqs, or GPU runtime settings."
            )

        api_type = self.api_type_combo.currentData() or "chat_completions"
        self.resolved_url_input.setText(
            resolve_request_url(
                self.endpoint_input.text(),
                api_type,
                self.auto_complete_check.isChecked(),
            )
        )
        if is_local:
            profile = self._preview_profile()
            self.command_preview.setPlainText(build_launch_command(profile))
        else:
            self.command_preview.setPlainText(
                "Runtime parameters are not sent to an external API."
            )

    def _toggle_runtime_advanced(self, expanded: bool) -> None:
        self.runtime_toggle.setText(
            "▾ Advanced runtime settings" if expanded else "▸ Advanced runtime settings"
        )
        self._refresh_derived_fields()

    def _preview_profile(self) -> Dict[str, Any]:
        return {
            "mode": self.mode_combo.currentData() or "external",
            "base_url": self.endpoint_input.text().strip(),
            "served_model": self.served_model_input.text().strip(),
            "model": self.local_model_input.text().strip(),
            "runtime": {
                "max_model_len": self.max_model_len_input.value(),
                "max_num_seqs": self.max_num_seqs_input.value(),
                "tensor_parallel_size": self.tensor_parallel_input.value(),
                "gpu_memory_utilization": self.gpu_memory_input.value(),
                "dtype": self.dtype_combo.currentData() or "auto",
                "quantization": self.quantization_combo.currentData() or "auto",
            },
        }

    def _toggle_api_key_visibility(self, visible: bool) -> None:
        self.api_key_input.setEchoMode(
            QtWidgets.QLineEdit.Normal if visible else QtWidgets.QLineEdit.Password
        )
        self.show_api_key_btn.setText("Hide" if visible else "Show")

    def _copy_command(self) -> None:
        if self.mode_combo.currentData() != "local":
            return
        QtWidgets.QApplication.clipboard().setText(self.command_preview.toPlainText())

    @staticmethod
    def _set_combo_value(
        combo: QtWidgets.QComboBox, value: str, allow_custom: bool = False
    ) -> None:
        index = combo.findData(value)
        if index < 0 and allow_custom:
            combo.addItem(f"Custom: {value}", value)
            index = combo.count() - 1
        combo.setCurrentIndex(max(index, 0))
