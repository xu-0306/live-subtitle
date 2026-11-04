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
from qfluentwidgets import (
    BodyLabel, CheckBox, ComboBox, FluentIcon, LineEdit, ListWidget,
    PrimaryPushButton, PushButton, StrongBodyLabel, TogglePushButton,
)

from gui.fluent_kit import FreeTextComboBox, disclosure_button, make_card, muted_label


PROFILE_KINDS = (
    ("api", "OpenAI-compatible API"),
    ("ollama", "Ollama"),
    ("nllb", "NLLB local model"),
)


def _as_profile(value: Mapping[str, Any], index: int, kind: str | None = None) -> dict[str, Any]:
    profile = copy.deepcopy(dict(value))
    if any(str(value or "").lower() == "vllm" for value in (kind, profile.get("kind"), profile.get("engine"))):
        from backend.config_store import normalize_service_profile

        profile = normalize_service_profile({**profile, "engine": "vllm"}, index)
        kind = "api"
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
    # Availability is not inferred from the profile type. A saved NLLB
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
    """Editable list of API, Ollama and NLLB connections."""

    changed = QtCore.Signal()
    notice = QtCore.Signal(str)
    test_requested = QtCore.Signal(object)
    save_requested = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._profiles: list[dict[str, Any]] = []
        self._active_index = -1
        self._draft: dict[str, Any] | None = None
        self._saved_profiles: list[dict[str, Any]] = []
        self._loading = False
        self._test_busy = False
        self._selection_state: dict[str, str] | None = None
        self._selection_touched = False
        self._build_ui()
        self._connect_signals()

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        # Wrapped help text must sit in a plain vertical layout; box layouts
        # size it by height-for-width, form/horizontal rows do not.
        title_row = QtWidgets.QHBoxLayout()
        title_row.addWidget(StrongBodyLabel("Service connections"), 1)
        self.new_btn = PushButton(FluentIcon.ADD, "Add service")
        self.new_btn.setProperty("kind", "secondary")
        self.duplicate_btn = PushButton(FluentIcon.COPY, "Duplicate")
        self.duplicate_btn.setProperty("kind", "secondary")
        self.remove_btn = PushButton(FluentIcon.DELETE, "Remove")
        self.remove_btn.setProperty("kind", "danger")
        for button in (self.new_btn, self.duplicate_btn, self.remove_btn):
            title_row.addWidget(button)
        root.addLayout(title_row)
        root.addWidget(
            muted_label("Add an API or Ollama connection, then select it in the extension.")
        )

        self._body_layout = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.LeftToRight)
        self._body_layout.setSpacing(14)
        list_card = make_card("Saved services")
        self.profile_list = ListWidget()
        self.profile_list.setObjectName("serviceList")
        self.profile_list.setMinimumWidth(0)
        self.profile_list.setMinimumHeight(180)
        self.profile_list.setWordWrap(True)
        list_card.layout().addWidget(self.profile_list, 1)
        self._body_layout.addWidget(list_card, 1)

        editor_card = make_card("Connection details")
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        editor_card.layout().addLayout(form)
        self.kind_combo = ComboBox()
        for value, label in PROFILE_KINDS:
            self.kind_combo.addItem(label, userData=value)
        self.name_input = LineEdit()
        self.name_input.setPlaceholderText("Friendly name")
        self.endpoint_input = LineEdit()
        self.endpoint_input.setPlaceholderText("https://provider.example/v1")
        self.model_input = LineEdit()
        self.model_input.setPlaceholderText("Model ID or served model")
        self.api_type_label = BodyLabel("API type")
        self.api_type_combo = FreeTextComboBox()
        for value, label in (
            ("chat_completions", "Chat Completions"),
            ("responses", "Responses"),
            ("messages", "Messages"),
        ):
            self.api_type_combo.addItem(label, userData=value)
        self.auto_complete_check = CheckBox("Complete provider URL automatically")
        self.auto_complete_check.setToolTip(
            "Keep this enabled when the provider uses the standard API path."
        )
        self.api_key_input = LineEdit()
        self.api_key_input.setEchoMode(QtWidgets.QLineEdit.Password)
        self.api_key_input.setPlaceholderText("Optional; stored in user config")
        # Fluent's PasswordLineEdit only reveals while its eye button is held;
        # keep the persistent Show/Hide toggle this editor always had.
        self.show_key_btn = TogglePushButton("Show")
        key_row = QtWidgets.QWidget()
        key_layout = QtWidgets.QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self.api_key_input, 1)
        key_layout.addWidget(self.show_key_btn)
        self.availability_label = BodyLabel("Configured")
        self.availability_label.setObjectName("statusBadge")
        self.default_check = CheckBox("Use as desktop default for the next capture")
        self.default_check.setToolTip(
            "This changes the saved preference only. It does not claim an active service."
        )
        self.test_btn = PushButton(FluentIcon.SEND, "Test connection")
        self.test_btn.setProperty("kind", "secondary")
        self.editor_status = muted_label("Saved", small=True)
        self.save_btn = PrimaryPushButton(FluentIcon.SAVE, "Save")
        self.discard_btn = PushButton("Discard")
        self.discard_btn.setProperty("kind", "secondary")
        actions = QtWidgets.QWidget()
        action_layout = QtWidgets.QHBoxLayout(actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.addWidget(self.save_btn)
        action_layout.addWidget(self.discard_btn)
        action_layout.addWidget(self.test_btn)
        action_layout.addWidget(self.editor_status, 1)
        self.advanced = QtWidgets.QWidget()
        advanced_form = QtWidgets.QFormLayout(self.advanced)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        advanced_form.setHorizontalSpacing(14)
        advanced_form.setVerticalSpacing(10)
        self.source_input = LineEdit()
        self.source_input.setReadOnly(True)
        self.source_input.setPlaceholderText("Imported from shared catalogue")
        self.id_input = LineEdit()
        self.id_input.setReadOnly(True)
        advanced_form.addRow(BodyLabel("Stable ID"), self.id_input)
        advanced_form.addRow(BodyLabel("Source"), self.source_input)
        # Starts collapsed; the button shows/hides ``self.advanced``.
        self.advanced_toggle = disclosure_button("Advanced profile metadata", self.advanced)

        form.addRow(BodyLabel("Type"), self.kind_combo)
        form.addRow(BodyLabel("Name"), self.name_input)
        form.addRow(BodyLabel("Endpoint / host"), self.endpoint_input)
        form.addRow(BodyLabel("Model"), self.model_input)
        form.addRow(self.api_type_label, self.api_type_combo)
        form.addRow("", self.auto_complete_check)
        form.addRow(BodyLabel("API key"), key_row)
        form.addRow(BodyLabel("Availability"), self.availability_label)
        form.addRow("", self.default_check)
        form.addRow("", actions)
        toggle_row = QtWidgets.QHBoxLayout()
        toggle_row.addWidget(self.advanced_toggle)
        toggle_row.addStretch(1)
        form.addRow("", toggle_row)
        form.addRow("", self.advanced)
        self._body_layout.addWidget(editor_card, 2)
        root.addLayout(self._body_layout)
        root.addStretch(1)
        self._list_card = list_card
        self._editor_card = editor_card
        self._narrow_catalog = False

    def _connect_signals(self) -> None:
        self.profile_list.currentRowChanged.connect(self._select_profile)
        self.new_btn.clicked.connect(self._new_profile)
        self.duplicate_btn.clicked.connect(self._duplicate_profile)
        self.remove_btn.clicked.connect(self._remove_profile)
        self.test_btn.clicked.connect(self._test_profile)
        self.save_btn.clicked.connect(self.save_selected)
        self.discard_btn.clicked.connect(self.discard_selected)
        self.show_key_btn.toggled.connect(self._toggle_key)
        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        self.name_input.textChanged.connect(self._editor_changed)
        self.endpoint_input.textChanged.connect(self._editor_changed)
        self.model_input.textChanged.connect(self._editor_changed)
        self.api_type_combo.currentTextChanged.connect(self._editor_changed)
        self.auto_complete_check.toggled.connect(self._editor_changed)
        self.api_key_input.textChanged.connect(self._editor_changed)
        self.default_check.toggled.connect(self._editor_changed)

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
        self._active_index = -1
        self._draft = None
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
                available_ids = {profile["id"] for profile in self._profiles}
                if legacy_id not in available_ids:
                    legacy_profiles = legacy_vllm.get("profiles")
                    if isinstance(legacy_profiles, list):
                        legacy_ids = [
                            str(item.get("id") or f"profile-{index + 1}")
                            for index, item in enumerate(legacy_profiles)
                            if isinstance(item, Mapping)
                        ]
                    else:
                        legacy_ids = [str(legacy_vllm.get("id") or legacy_vllm.get("active_profile") or "legacy-api")]
                    legacy_id = next((identity for identity in legacy_ids if identity in available_ids), "")
                if legacy_id:
                    self._selection_state = {"kind": "profile", "id": legacy_id}
        self._selection_touched = False
        default_id = ""
        if self._selection_state and self._selection_state.get("kind") == "profile":
            default_id = self._selection_state.get("id", "")
        for profile in self._profiles:
            profile["_default"] = profile["id"] == default_id
        self._saved_profiles = copy.deepcopy(self._profiles)
        index = next((i for i, p in enumerate(self._profiles) if p["id"] == default_id), 0)
        self._active_index = index if self._profiles else -1
        self._draft = copy.deepcopy(self._profiles[index]) if self._profiles else None
        self._refresh_list()
        self._load_editor()
        self._set_editor_enabled(bool(self._profiles))

    def collect_into_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        self._store_editor()
        profiles = self._draft_profiles()
        selection_touched = self._selection_touched or any(
            p.get("_default") for p in profiles if p not in self._saved_profiles
        ) or any(p.get("_default") for p in self._saved_profiles if p not in profiles)
        updated = copy.deepcopy(dict(config))
        translation = updated.get("translation")
        translation_map = dict(translation) if isinstance(translation, Mapping) else {}
        original_profiles = translation_map.get("service_profiles", translation_map.get("profiles", []))
        originals = {str(p.get("id")): p for p in original_profiles if isinstance(p, Mapping)} if isinstance(original_profiles, list) else {}
        clean_profiles = []
        for profile in profiles:
            clean = {k: copy.deepcopy(v) for k, v in profile.items() if not k.startswith("_")}
            kind = str(clean.get("kind") or "api")
            engine = {
                "api": "openai_compatible",
                "ollama": "ollama",
                "nllb": "nllb",
            }.get(kind, kind)
            clean["engine"] = engine
            clean["endpoint"] = clean.get("base_url", "")
            if engine == "ollama":
                clean.pop("host", None)
            original = originals.get(profile["id"])
            if (profile in self._saved_profiles and original is not None
                    and "vllm" not in {original.get("kind"), original.get("engine")}):
                clean = copy.deepcopy(dict(original))
            clean_profiles.append(clean)
        translation_map["service_profiles"] = copy.deepcopy(clean_profiles)
        translation_map["profiles"] = clean_profiles
        default = next((p["id"] for p in profiles if p.get("_default")), "")
        if default:
            if selection_touched or str(translation_map.get("engine") or "").lower() == "vllm":
                translation_map["default_selection"] = {"kind": "profile", "id": default}
                translation_map.pop("selection", None)
            # Remove the GUI-only legacy key after the first canonical save.
            translation_map.pop("default_profile_id", None)
        elif selection_touched:
            # Keep an explicit None selection so a previously selected service
            # is cleared instead of being re-inferred by the backend.
            translation_map["default_selection"] = {"kind": "none", "id": "none"}
            translation_map.pop("selection", None)
            translation_map.pop("default_profile_id", None)
        else:
            # A legacy key cannot select a service in the current backend
            # contract and should not survive a normal noop/default save.
            translation_map.pop("default_profile_id", None)
        # Connections now live in the shared catalogue. Do not keep a second
        # profile store containing the removed helper's launch parameters.
        if str(translation_map.get("engine") or "").lower() == "vllm":
            translation_map["engine"] = "noop"
        translation_map.pop("vllm", None)
        # Keep legacy adapters in sync for old extension/backend versions while
        # the canonical profile list is being adopted.
        for kind, section_name in (("ollama", "ollama"), ("nllb", "nllb"), ("api", "openai")):
            selected = next((p for p in profiles if p.get("kind") == kind), None)
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

    def _draft_profiles(self) -> list[dict[str, Any]]:
        profiles = copy.deepcopy(self._profiles)
        if self._draft is not None and 0 <= self._active_index < len(profiles):
            profiles[self._active_index] = copy.deepcopy(self._draft)
            if self._draft.get("_default"):
                for index, profile in enumerate(profiles):
                    if index != self._active_index:
                        profile["_default"] = False
        return profiles

    def has_unsaved_changes(self) -> bool:
        return self._draft_profiles() != self._saved_profiles

    def save_selected(self) -> bool:
        if self.has_unsaved_changes():
            self.save_requested.emit()
        return not self.has_unsaved_changes()

    def mark_saved(self) -> None:
        self._selection_touched = self._selection_touched or any(
            p.get("_default") for p in self._draft_profiles() if p not in self._saved_profiles
        ) or any(p.get("_default") for p in self._saved_profiles if p not in self._draft_profiles())
        self._profiles = self._draft_profiles()
        self._saved_profiles = copy.deepcopy(self._profiles)
        self._refresh_list()
        self.editor_status.setText("Saved")
        self.changed.emit()

    def discard_selected(self) -> None:
        self._profiles = copy.deepcopy(self._saved_profiles)
        self._active_index = min(self._active_index, len(self._profiles) - 1)
        self._draft = copy.deepcopy(self._profiles[self._active_index]) if self._active_index >= 0 else None
        self._refresh_list()
        self._load_editor()
        self.changed.emit()

    def _guard_draft(self) -> bool:
        if not self.has_unsaved_changes():
            return True
        answer = QtWidgets.QMessageBox.question(
            self, "Unsaved service", "Save changes to this service before continuing?",
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        if answer == QtWidgets.QMessageBox.Save:
            return self.save_selected()
        if answer == QtWidgets.QMessageBox.Discard:
            self.discard_selected()
            return True
        return False

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
        for index, profile in enumerate(self._profiles):
            state = self._profile_state(profile)
            item = QtWidgets.QListWidgetItem(f"{profile['name']}\n{profile['kind'].upper()} · {state}")
            if index == self._active_index and self.has_unsaved_changes():
                item.setText(item.text() + " · Unsaved")
            item.setData(QtCore.Qt.UserRole, profile["id"])
            self.profile_list.addItem(item)
        if self._profiles:
            self.profile_list.setCurrentRow(max(0, min(row, len(self._profiles) - 1)))
        self.profile_list.blockSignals(False)
        self.remove_btn.setEnabled(bool(self._profiles))
        self.duplicate_btn.setEnabled(bool(self._profiles))
        dirty = self.has_unsaved_changes()
        self.editor_status.setText("Unsaved changes" if dirty else "Saved")
        self.save_btn.setEnabled(dirty)
        self.discard_btn.setEnabled(dirty)

    def _select_profile(self, index: int) -> None:
        if self._loading:
            return
        if index == self._active_index:
            return
        if not self._guard_draft():
            self.profile_list.blockSignals(True)
            self.profile_list.setCurrentRow(self._active_index)
            self.profile_list.blockSignals(False)
            return
        self._active_index = index
        self._draft = copy.deepcopy(self._profiles[index]) if 0 <= index < len(self._profiles) else None
        self._refresh_list()
        self._load_editor()

    def _load_editor(self) -> None:
        profile = self._draft
        self._loading = True
        try:
            if profile is None:
                for field in (self.name_input, self.endpoint_input, self.model_input,
                              self.api_key_input, self.id_input, self.source_input):
                    field.clear()
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
                self.api_type_combo.addItem(f"{api_type} (custom)", userData=api_type)
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
        profile = self._draft
        if profile is None:
            return
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

    def _editor_changed(self, *_args: object) -> None:
        if self._loading:
            return
        self._store_editor()
        self._refresh_list()
        self.changed.emit()

    def _kind_changed(self) -> None:
        if self._loading or self._draft is None:
            return
        kind = self.kind_combo.currentData() or "api"
        if kind == self._draft.get("kind"):
            return
        if self._profiles[self._active_index]["id"] in {p["id"] for p in self._saved_profiles}:
            answer = QtWidgets.QMessageBox.question(
                self, "Change service type",
                "Changing Type creates a new service ID and clears connection fields. Continue?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Cancel,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                self._load_editor()
                return
        self._draft = _as_profile({
            "id": f"service-{uuid.uuid4().hex[:8]}",
            "name": self._draft["name"], "kind": kind,
            "_default": self._draft.get("_default", False),
        }, self._active_index)
        self._load_editor()
        self._editor_changed()

    def _new_profile(self) -> None:
        if not self._guard_draft():
            return
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
        self._draft = copy.deepcopy(profile)
        self._refresh_list()
        self.profile_list.setCurrentRow(self._active_index)
        self._load_editor()
        self.changed.emit()

    def _duplicate_profile(self) -> None:
        if not (0 <= self._active_index < len(self._profiles)):
            return
        if not self._guard_draft():
            return
        profile = copy.deepcopy(self._profiles[self._active_index])
        profile["id"] = f"service-{uuid.uuid4().hex[:8]}"
        profile["name"] = f"{profile.get('name') or 'Translation service'} copy"
        profile["_default"] = False
        self._profiles.append(profile)
        self._active_index = len(self._profiles) - 1
        self._draft = copy.deepcopy(profile)
        self._refresh_list()
        self.profile_list.setCurrentRow(self._active_index)
        self._load_editor()
        self.changed.emit()

    def _remove_profile(self) -> None:
        if not (0 <= self._active_index < len(self._profiles)):
            return
        if self._draft != self._profiles[self._active_index] and not self._guard_draft():
            return
        removed = self._profiles.pop(self._active_index)
        if removed.get("_default"):
            self._selection_touched = True
            self._selection_state = {"kind": "none", "id": "none"}
            for profile in self._profiles:
                profile["_default"] = False
        self._active_index = min(self._active_index, len(self._profiles) - 1)
        self._draft = copy.deepcopy(self._profiles[self._active_index]) if self._active_index >= 0 else None
        self._refresh_list()
        self._load_editor()
        self.changed.emit()

    def _test_profile(self) -> None:
        profile = self._draft
        if profile is None:
            self.notice.emit("Select a saved service before testing it.")
            return
        self._store_editor()
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id:
            self.notice.emit("Save this service with a stable ID before testing it.")
            return
        self.test_requested.emit(copy.deepcopy(profile))

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
        }
        model_placeholders = {
            "api": "Model ID, for example gpt-4o-mini",
            "ollama": "Ollama model, for example llama3.1",
            "nllb": "NLLB model ID or local model name",
        }
        self.endpoint_input.setPlaceholderText(endpoint_placeholders.get(kind, endpoint_placeholders["api"]))
        self.model_input.setPlaceholderText(model_placeholders.get(kind, model_placeholders["api"]))
        provider_fields = kind == "api"
        self.api_type_label.setVisible(provider_fields)
        self.api_type_combo.setVisible(provider_fields)
        self.auto_complete_check.setVisible(provider_fields)
        self.api_key_input.setEnabled(kind in {"api", "ollama"})
        self.show_key_btn.setEnabled(kind in {"api", "ollama"})

    @staticmethod
    def _profile_state(profile: Mapping[str, Any]) -> str:
        if not profile.get("available", True):
            return "Missing"
        kind = str(profile.get("kind") or "api")
        endpoint = str(profile.get("base_url") or "").strip()
        model = str(profile.get("model") or "").strip()
        if kind == "nllb":
            return "Installed" if profile.get("installed") and model else "Needs setup"
        if kind in {"api", "ollama"}:
            return "Configured" if endpoint and model else "Needs setup"
        return "Configured" if endpoint or model else "Needs setup"
