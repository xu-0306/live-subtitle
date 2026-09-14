"""Unified translation-service editor used by the desktop GUI.

The backend owns the canonical catalogue.  This widget keeps a lossless local
editing model so older ``translation.vllm``, ``translation.ollama`` and
``translation.nllb`` configurations remain visible while the shared catalogue
is being migrated.  It never treats the selected row as an active inference
process; the row is only the next-capture preference.
"""
from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets


PROFILE_KINDS = (
    ("api", "OpenAI-compatible API"),
    ("ollama", "Ollama"),
    ("nllb", "NLLB local model"),
    ("vllm", "vLLM helper"),
)


def _as_profile(value: Mapping[str, Any], index: int, kind: str | None = None) -> dict[str, Any]:
    profile = copy.deepcopy(dict(value))
    profile["id"] = str(profile.get("id") or f"service-{index + 1}")
    profile["name"] = str(profile.get("name") or f"Translation service {index + 1}")
    engine = str(profile.get("engine") or "").strip().lower()
    kind_aliases = {
        "openai": "api",
        "openai_compatible": "api",
        "managed_llama": "nllb",
    }
    profile["kind"] = str(kind or profile.get("kind") or kind_aliases.get(engine, engine) or "api")
    profile["base_url"] = str(
        profile.get("base_url") or profile.get("endpoint") or profile.get("host") or ""
    )
    profile["model"] = str(profile.get("model") or profile.get("served_model") or "")
    profile["api_key"] = str(profile.get("api_key") or "")
    profile["api_type"] = str(profile.get("api_type") or "chat_completions")
    profile["auto_complete_endpoint"] = bool(profile.get("auto_complete_endpoint", True))
    # Availability is not inferred from the profile type. A saved NLLB/vLLM
    # row can still be missing its model, and an Ollama/API row can be merely
    # configured until a read-only test succeeds.
    profile["installed"] = bool(profile.get("installed", False))
    profile["available"] = bool(profile.get("available", True))
    return profile


def profiles_from_config(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read canonical and legacy profile shapes without dropping fields."""

    translation = config.get("translation")
    translation_map = translation if isinstance(translation, Mapping) else {}
    output: list[dict[str, Any]] = []
    has_explicit_catalog = False
    canonical = translation_map.get("service_profiles")
    if isinstance(canonical, list):
        has_explicit_catalog = True
    else:
        canonical = translation_map.get("profiles")
        has_explicit_catalog = isinstance(canonical, list)
    if isinstance(canonical, list):
        for index, value in enumerate(canonical):
            if isinstance(value, Mapping):
                output.append(_as_profile(value, index))

    # The shared backend storage adapter is authoritative for normalized
    # imported profiles, but it may also expose only the legacy vLLM adapter
    # while older config sections still contain Ollama/NLLB/API entries.  Add
    # those entries to the same catalogue and de-duplicate by stable ID below.
    # Keep this import optional so the GUI remains usable with an older
    # checkout during migration.
    try:
        from backend.config_store import service_profiles as shared_profiles
    except ImportError:
        shared_profiles = None
    if callable(shared_profiles) and not has_explicit_catalog:
        try:
            shared = shared_profiles(config)
        except (TypeError, ValueError):
            shared = []
        existing_ids = {str(item.get("id")) for item in output}
        for index, profile in enumerate(shared):
            if not isinstance(profile, Mapping):
                continue
            normalized = _as_profile(profile, len(output) + index)
            if normalized["id"] not in existing_ids:
                output.append(normalized)
                existing_ids.add(normalized["id"])

    # Legacy sections are useful during migration when there is no explicit
    # canonical list. If a canonical list exists, it is the user's complete
    # catalogue and should remain lossless rather than gaining implicit rows.
    if not has_explicit_catalog:
        vllm = translation_map.get("vllm")
        values = vllm.get("profiles") if isinstance(vllm, Mapping) else None
        if isinstance(values, list):
            for index, value in enumerate(values):
                if isinstance(value, Mapping):
                    profile = _as_profile(value, index, "vllm")
                    if not any(item.get("id") == profile["id"] for item in output):
                        output.append(profile)
        for key, kind, label in (
            ("ollama", "ollama", "Ollama"),
            ("nllb", "nllb", "NLLB"),
            ("openai", "api", "OpenAI API"),
        ):
            section = translation_map.get(key)
            if isinstance(section, Mapping):
                item = dict(section)
                item.setdefault("id", f"{kind}-default")
                item.setdefault("name", label)
                profile = _as_profile(item, len(output), kind)
                if not any(existing.get("id") == profile["id"] for existing in output):
                    output.append(profile)
    # An explicit empty canonical list is meaningful: the user removed every
    # service and selected ``none``.  Only synthesize a starter row for older
    # configs that have no catalogue at all; otherwise a reload would silently
    # resurrect a service the user just removed.
    if not output and not has_explicit_catalog:
        output.append(
            _as_profile(
                {
                    "id": "api-default",
                    "name": "Desktop default",
                    "kind": "api",
                    "base_url": "",
                    "model": "",
                    "available": True,
                },
                0,
            )
        )
    # Keep IDs stable and prevent an imported duplicate from making selection
    # ambiguous.  The second item gets a deterministic suffix without using a
    # provider-specific name or a closed list of models.
    seen: set[str] = set()
    for profile in output:
        original = profile["id"]
        candidate = original
        suffix = 2
        while candidate in seen:
            candidate = f"{original}-{suffix}"
            suffix += 1
        profile["id"] = candidate
        seen.add(candidate)
    return output


class ServiceCatalogWidget(QtWidgets.QWidget):
    """Editable list of API, Ollama, NLLB and vLLM profiles."""

    changed = QtCore.Signal()
    notice = QtCore.Signal(str)
    test_requested = QtCore.Signal(object)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._profiles: list[dict[str, Any]] = []
        self._active_index = -1
        self._loading = False
        self._test_busy = False
        self._selection_state: dict[str, str] | None = None
        self._selection_touched = False
        self._build_ui()
        self._connect_signals()

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        header = QtWidgets.QFrame()
        header.setObjectName("pageHeaderCard")
        header_layout = QtWidgets.QVBoxLayout(header)
        title = QtWidgets.QLabel("Service connections")
        title.setObjectName("cardTitle")
        subtitle = QtWidgets.QLabel(
            "PROFILES · CONFIGURATION ONLY\n"
            "Configure cloud or self-hosted connections such as APIs, Ollama and vLLM, plus legacy NLLB adapters. "
            "Managed llama.cpp downloads and on-device model files stay under Local translation."
        )
        subtitle.setObjectName("mutedText")
        subtitle.setWordWrap(True)
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        root.addWidget(header)

        toolbar = QtWidgets.QHBoxLayout()
        self.catalog_status = QtWidgets.QLabel("Connections & adapters")
        self.catalog_status.setObjectName("statusBadge")
        toolbar.addWidget(self.catalog_status)
        toolbar.addStretch(1)
        self.new_btn = QtWidgets.QPushButton("Add service")
        self.duplicate_btn = QtWidgets.QPushButton("Duplicate")
        self.remove_btn = QtWidgets.QPushButton("Remove")
        self.remove_btn.setProperty("kind", "danger")
        toolbar.addWidget(self.new_btn)
        toolbar.addWidget(self.duplicate_btn)
        toolbar.addWidget(self.remove_btn)
        root.addLayout(toolbar)

        self._body_layout = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.LeftToRight)
        self._body_layout.setSpacing(12)
        list_card = QtWidgets.QFrame()
        list_card.setObjectName("card")
        list_layout = QtWidgets.QVBoxLayout(list_card)
        list_title = QtWidgets.QLabel("Saved services")
        list_title.setObjectName("cardTitleSmall")
        list_layout.addWidget(list_title)
        self.profile_list = QtWidgets.QListWidget()
        self.profile_list.setObjectName("serviceList")
        self.profile_list.setMinimumWidth(0)
        self.profile_list.setWordWrap(True)
        list_layout.addWidget(self.profile_list, 1)
        self._body_layout.addWidget(list_card, 1)

        editor_card = QtWidgets.QFrame()
        editor_card.setObjectName("card")
        form = QtWidgets.QFormLayout(editor_card)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self.kind_combo = QtWidgets.QComboBox()
        for value, label in PROFILE_KINDS:
            self.kind_combo.addItem(label, value)
        self.name_input = QtWidgets.QLineEdit()
        self.name_input.setPlaceholderText("Friendly name")
        self.endpoint_input = QtWidgets.QLineEdit()
        self.endpoint_input.setPlaceholderText("https://provider.example/v1")
        self.model_input = QtWidgets.QLineEdit()
        self.model_input.setPlaceholderText("Model ID or served model")
        self.api_type_label = QtWidgets.QLabel("API type")
        self.api_type_combo = QtWidgets.QComboBox()
        self.api_type_combo.setEditable(True)
        for value, label in (
            ("chat_completions", "Chat Completions"),
            ("responses", "Responses"),
            ("messages", "Messages"),
        ):
            self.api_type_combo.addItem(label, value)
        self.auto_complete_check = QtWidgets.QCheckBox("Complete provider URL automatically")
        self.auto_complete_check.setToolTip(
            "Keep this enabled when the provider uses the standard API path."
        )
        self.api_key_input = QtWidgets.QLineEdit()
        self.api_key_input.setEchoMode(QtWidgets.QLineEdit.Password)
        self.api_key_input.setPlaceholderText("Optional; stored in user config")
        self.show_key_btn = QtWidgets.QToolButton()
        self.show_key_btn.setText("Show")
        self.show_key_btn.setCheckable(True)
        key_row = QtWidgets.QWidget()
        key_layout = QtWidgets.QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self.api_key_input, 1)
        key_layout.addWidget(self.show_key_btn)
        self.availability_label = QtWidgets.QLabel("Configured")
        self.availability_label.setObjectName("statusBadge")
        self.default_check = QtWidgets.QCheckBox("Use as desktop default for the next capture")
        self.default_check.setToolTip(
            "This changes the saved preference only. It does not claim an active service."
        )
        self.test_btn = QtWidgets.QPushButton("Test connection")
        self.test_btn.setProperty("kind", "secondary")
        self.advanced_toggle = QtWidgets.QToolButton()
        self.advanced_toggle.setText("Advanced profile metadata")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced = QtWidgets.QWidget()
        advanced_form = QtWidgets.QFormLayout(self.advanced)
        self.source_input = QtWidgets.QLineEdit()
        self.source_input.setReadOnly(True)
        self.source_input.setPlaceholderText("Imported from shared catalogue")
        self.id_input = QtWidgets.QLineEdit()
        self.id_input.setReadOnly(True)
        advanced_form.addRow("Stable ID", self.id_input)
        advanced_form.addRow("Source", self.source_input)
        self.advanced.setVisible(False)

        form.addRow("Type", self.kind_combo)
        form.addRow("Name", self.name_input)
        form.addRow("Endpoint / host", self.endpoint_input)
        form.addRow("Model", self.model_input)
        form.addRow(self.api_type_label, self.api_type_combo)
        form.addRow("", self.auto_complete_check)
        form.addRow("API key", key_row)
        form.addRow("Availability", self.availability_label)
        form.addRow("", self.default_check)
        form.addRow("", self.test_btn)
        form.addRow("", self.advanced_toggle)
        form.addRow("", self.advanced)
        self._body_layout.addWidget(editor_card, 2)
        root.addLayout(self._body_layout, 1)
        self._list_card = list_card
        self._editor_card = editor_card
        self._narrow_catalog = False

    def _connect_signals(self) -> None:
        self.profile_list.currentRowChanged.connect(self._select_profile)
        self.new_btn.clicked.connect(self._new_profile)
        self.duplicate_btn.clicked.connect(self._duplicate_profile)
        self.remove_btn.clicked.connect(self._remove_profile)
        self.test_btn.clicked.connect(self._test_profile)
        self.show_key_btn.toggled.connect(self._toggle_key)
        self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        self.kind_combo.currentIndexChanged.connect(self._editor_changed)
        self.kind_combo.currentIndexChanged.connect(self._update_kind_fields)
        self.name_input.textChanged.connect(self._editor_changed)
        self.endpoint_input.textChanged.connect(self._editor_changed)
        self.model_input.textChanged.connect(self._editor_changed)
        self.api_type_combo.currentTextChanged.connect(self._editor_changed)
        self.auto_complete_check.toggled.connect(self._editor_changed)
        self.api_key_input.textChanged.connect(self._editor_changed)
        self.default_check.toggled.connect(self._editor_changed)
        self.default_check.toggled.connect(self._default_toggled)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        """Stack the catalogue editor when the 900x640 window is narrow."""

        # The page has a sidebar and outer margins, leaving roughly 650px in
        # the minimum supported window. A vertical editor keeps every control
        # reachable instead of forcing a clipped horizontal form.
        narrow = self.width() < 760
        if narrow != self._narrow_catalog:
            self._narrow_catalog = narrow
            direction = (
                QtWidgets.QBoxLayout.TopToBottom
                if narrow
                else QtWidgets.QBoxLayout.LeftToRight
            )
            self._body_layout.setDirection(direction)
        super().resizeEvent(event)

    def load_from_config(self, config: Mapping[str, Any]) -> None:
        self._profiles = profiles_from_config(config)
        translation = config.get("translation")
        translation_map = translation if isinstance(translation, Mapping) else {}
        selection = translation_map.get("default_selection")
        if not isinstance(selection, Mapping):
            selection = translation_map.get("selection")
        self._selection_state = None
        if isinstance(selection, Mapping):
            kind = str(selection.get("kind") or "").strip()
            identity = str(selection.get("id") or "").strip()
            if kind and identity:
                self._selection_state = {"kind": kind, "id": identity}
        # The old vLLM adapter is safe to infer only while vLLM is the actual
        # configured engine. A noop config may still contain an unused legacy
        # vLLM profile and must not silently select it for the next capture.
        if self._selection_state is None and str(translation_map.get("engine") or "").lower() == "vllm":
            legacy_vllm = translation_map.get("vllm")
            if isinstance(legacy_vllm, Mapping):
                legacy_id = str(legacy_vllm.get("active_profile") or "").strip()
                if legacy_id:
                    self._selection_state = {"kind": "profile", "id": legacy_id}
        self._selection_touched = False
        default_id = ""
        if self._selection_state and self._selection_state.get("kind") == "profile":
            default_id = self._selection_state.get("id", "")
        for profile in self._profiles:
            profile["_default"] = profile["id"] == default_id
        self._refresh_list()
        index = next((i for i, p in enumerate(self._profiles) if p["id"] == default_id), 0)
        self.profile_list.setCurrentRow(index)
        self._set_editor_enabled(bool(self._profiles))

    def collect_into_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        self._store_editor()
        updated = copy.deepcopy(dict(config))
        translation = updated.get("translation")
        translation_map = dict(translation) if isinstance(translation, Mapping) else {}
        clean_profiles = []
        for profile in self._profiles:
            clean = {k: copy.deepcopy(v) for k, v in profile.items() if not k.startswith("_")}
            kind = str(clean.get("kind") or "api")
            engine = {
                "api": "openai_compatible",
                "ollama": "ollama",
                "nllb": "nllb",
                "vllm": "vllm",
            }.get(kind, kind)
            clean["engine"] = engine
            clean["endpoint"] = clean.get("base_url", "")
            clean_profiles.append(clean)
        translation_map["service_profiles"] = copy.deepcopy(clean_profiles)
        translation_map["profiles"] = clean_profiles
        default = next((p["id"] for p in self._profiles if p.get("_default")), "")
        if default:
            if self._selection_touched:
                translation_map["default_selection"] = {"kind": "profile", "id": default}
                translation_map.pop("selection", None)
            # Remove the GUI-only legacy key after the first canonical save.
            translation_map.pop("default_profile_id", None)
        elif self._selection_touched:
            # Keep an explicit None selection so a previously selected service
            # is cleared instead of being re-inferred by the backend.
            translation_map["default_selection"] = {"kind": "none", "id": "none"}
            translation_map.pop("selection", None)
            translation_map.pop("default_profile_id", None)
        else:
            # A legacy key cannot select a service in the current backend
            # contract and should not survive a normal noop/default save.
            translation_map.pop("default_profile_id", None)
        # Keep legacy adapters in sync for old extension/backend versions while
        # the canonical profile list is being adopted.
        for kind, section_name in (("ollama", "ollama"), ("nllb", "nllb"), ("api", "openai")):
            selected = next((p for p in self._profiles if p.get("kind") == kind), None)
            if selected:
                legacy = dict(translation_map.get(section_name) or {})
                legacy.update(
                    {
                        "host" if kind == "ollama" else "base_url": selected.get("base_url", ""),
                        "model": selected.get("model", ""),
                        "api_key": selected.get("api_key", ""),
                    }
                )
                if kind == "api":
                    legacy["api_type"] = selected.get("api_type", "chat_completions")
                    legacy["auto_complete_endpoint"] = bool(
                        selected.get("auto_complete_endpoint", True)
                    )
                translation_map[section_name] = legacy
        updated["translation"] = translation_map
        return updated

    def _set_editor_enabled(self, enabled: bool) -> None:
        for widget in (
            self.kind_combo,
            self.name_input,
            self.endpoint_input,
            self.model_input,
            self.api_key_input,
            self.api_type_combo,
            self.auto_complete_check,
            self.default_check,
            self.test_btn,
        ):
            widget.setEnabled(enabled)
        self.test_btn.setEnabled(bool(enabled) and not self._test_busy)

    def set_test_busy(self, busy: bool) -> None:
        self._test_busy = bool(busy)
        self.test_btn.setText("Testing…" if self._test_busy else "Test connection")
        self.test_btn.setEnabled(bool(self._profiles) and not self._test_busy)

    def _refresh_list(self) -> None:
        row = self._active_index if self._active_index >= 0 else self.profile_list.currentRow()
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        for profile in self._profiles:
            state = self._profile_state(profile)
            item = QtWidgets.QListWidgetItem(f"{profile['name']}\n{profile['kind'].upper()} · {state}")
            item.setData(QtCore.Qt.UserRole, profile["id"])
            self.profile_list.addItem(item)
        self.profile_list.blockSignals(False)
        if self._profiles:
            self.profile_list.setCurrentRow(max(0, min(row, len(self._profiles) - 1)))
        self.remove_btn.setEnabled(bool(self._profiles))
        self.duplicate_btn.setEnabled(bool(self._profiles))

    def _select_profile(self, index: int) -> None:
        if self._loading:
            return
        self._store_editor()
        self._active_index = index
        self._load_editor()

    def _load_editor(self) -> None:
        profile = self._profiles[self._active_index] if 0 <= self._active_index < len(self._profiles) else None
        self._loading = True
        try:
            if profile is None:
                self._set_editor_enabled(False)
                return
            self._set_editor_enabled(True)
            index = self.kind_combo.findData(profile.get("kind", "api"))
            self.kind_combo.setCurrentIndex(max(0, index))
            self.name_input.setText(str(profile.get("name") or ""))
            self.endpoint_input.setText(str(profile.get("base_url") or ""))
            self.model_input.setText(str(profile.get("model") or ""))
            self.api_key_input.setText(str(profile.get("api_key") or ""))
            api_type = str(profile.get("api_type") or "chat_completions")
            api_index = self.api_type_combo.findData(api_type)
            if api_index < 0:
                self.api_type_combo.addItem(f"{api_type} (custom)", api_type)
                api_index = self.api_type_combo.count() - 1
            self.api_type_combo.setCurrentIndex(api_index)
            self.auto_complete_check.setChecked(bool(profile.get("auto_complete_endpoint", True)))
            self.availability_label.setText(self._profile_state(profile))
            self.default_check.setChecked(bool(profile.get("_default")))
            self.id_input.setText(str(profile.get("id") or ""))
            self.source_input.setText(str(profile.get("source") or "Shared settings"))
            self._update_kind_fields()
        finally:
            self._loading = False

    def _store_editor(self) -> None:
        if self._loading or not (0 <= self._active_index < len(self._profiles)):
            return
        profile = self._profiles[self._active_index]
        api_type_text = self.api_type_combo.currentText().strip()
        api_type_index = self.api_type_combo.currentIndex()
        api_type_data = (
            self.api_type_combo.itemData(api_type_index)
            if api_type_index >= 0
            else None
        )
        # Standard and preserved custom entries display a friendly label while
        # storing their protocol value in userData.  Free-form edits have no
        # matching item and should keep the text exactly as entered.
        if (
            api_type_data
            and api_type_index >= 0
            and api_type_text == self.api_type_combo.itemText(api_type_index)
        ):
            api_type = str(api_type_data).strip()
        else:
            api_type = api_type_text or str(api_type_data or "chat_completions").strip()
        profile.update(
            {
                "kind": self.kind_combo.currentData() or "api",
                "name": self.name_input.text().strip() or f"Translation service {self._active_index + 1}",
                "base_url": self.endpoint_input.text().strip(),
                "model": self.model_input.text().strip(),
                "api_key": self.api_key_input.text(),
                "api_type": api_type or "chat_completions",
                "auto_complete_endpoint": self.auto_complete_check.isChecked(),
                "_default": self.default_check.isChecked(),
            }
        )
        if profile["_default"]:
            for index, other in enumerate(self._profiles):
                if index != self._active_index:
                    other["_default"] = False

    def _editor_changed(self, *_args: object) -> None:
        if self._loading:
            return
        self._store_editor()
        row = self.profile_list.currentRow()
        if 0 <= row < self.profile_list.count():
            profile = self._profiles[row]
            state = self._profile_state(profile)
            self.profile_list.item(row).setText(f"{profile['name']}\n{profile['kind'].upper()} · {state}")
        self.changed.emit()

    def _new_profile(self) -> None:
        self._store_editor()
        profile = _as_profile(
            {
                "id": f"service-{uuid.uuid4().hex[:8]}",
                "name": f"Translation service {len(self._profiles) + 1}",
                "kind": "api",
                "available": True,
            },
            len(self._profiles),
        )
        profile["_default"] = False
        self._profiles.append(profile)
        self._active_index = len(self._profiles) - 1
        self._refresh_list()
        self.profile_list.setCurrentRow(self._active_index)
        self._load_editor()
        self.changed.emit()

    def _duplicate_profile(self) -> None:
        if not (0 <= self._active_index < len(self._profiles)):
            return
        self._store_editor()
        profile = copy.deepcopy(self._profiles[self._active_index])
        profile["id"] = f"service-{uuid.uuid4().hex[:8]}"
        profile["name"] = f"{profile.get('name') or 'Translation service'} copy"
        profile["_default"] = False
        self._profiles.append(profile)
        self._active_index = len(self._profiles) - 1
        self._refresh_list()
        self.profile_list.setCurrentRow(self._active_index)
        self._load_editor()
        self.changed.emit()

    def _remove_profile(self) -> None:
        if not (0 <= self._active_index < len(self._profiles)):
            return
        removed = self._profiles.pop(self._active_index)
        if removed.get("_default"):
            self._selection_touched = True
            self._selection_state = {"kind": "none", "id": "none"}
            for profile in self._profiles:
                profile["_default"] = False
        self._active_index = min(self._active_index, len(self._profiles) - 1)
        self._refresh_list()
        self._load_editor()
        self.changed.emit()

    def _test_profile(self) -> None:
        profile = self._profiles[self._active_index] if 0 <= self._active_index < len(self._profiles) else None
        if profile is None:
            self.notice.emit("Select a saved service before testing it.")
            return
        self._store_editor()
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id:
            self.notice.emit("Save this service with a stable ID before testing it.")
            return
        self.test_requested.emit(copy.deepcopy(profile))

    def _default_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._selection_touched = True
        if checked:
            self._selection_state = {
                "kind": "profile",
                "id": str(self._profiles[self._active_index].get("id") or ""),
            }
        else:
            self._selection_state = {"kind": "none", "id": "none"}

    def _toggle_key(self, visible: bool) -> None:
        self.api_key_input.setEchoMode(QtWidgets.QLineEdit.Normal if visible else QtWidgets.QLineEdit.Password)
        self.show_key_btn.setText("Hide" if visible else "Show")

    def _update_kind_fields(self, *_args: object) -> None:
        """Keep provider-specific guidance visible without separate editors."""

        kind = str(self.kind_combo.currentData() or "api")
        endpoint_placeholders = {
            "api": "https://provider.example/v1",
            "ollama": "http://127.0.0.1:11434",
            "nllb": "Local model path or managed endpoint",
            "vllm": "http://127.0.0.1:8000",
        }
        model_placeholders = {
            "api": "Model ID, for example gpt-4o-mini",
            "ollama": "Ollama model, for example llama3.1",
            "nllb": "NLLB model ID or local model name",
            "vllm": "Served model name",
        }
        self.endpoint_input.setPlaceholderText(endpoint_placeholders.get(kind, endpoint_placeholders["api"]))
        self.model_input.setPlaceholderText(model_placeholders.get(kind, model_placeholders["api"]))
        provider_fields = kind in {"api", "vllm"}
        self.api_type_label.setVisible(provider_fields)
        self.api_type_combo.setVisible(provider_fields)
        self.auto_complete_check.setVisible(provider_fields)
        self.api_key_input.setEnabled(kind in {"api", "ollama", "vllm"})
        self.show_key_btn.setEnabled(kind in {"api", "ollama", "vllm"})

    @staticmethod
    def _profile_state(profile: Mapping[str, Any]) -> str:
        if not profile.get("available", True):
            return "Missing"
        kind = str(profile.get("kind") or "api")
        endpoint = str(profile.get("base_url") or "").strip()
        model = str(profile.get("model") or "").strip()
        if kind == "nllb":
            return "Installed" if profile.get("installed") and model else "Needs setup"
        if kind == "vllm":
            return "Configured" if endpoint and model else "Needs setup"
        if kind in {"api", "ollama"}:
            return "Configured" if endpoint and model else "Needs setup"
        return "Configured" if endpoint or model else "Needs setup"
