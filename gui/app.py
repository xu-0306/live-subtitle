from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6 import QtCore, QtGui, QtWebSockets, QtWidgets
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CheckBox, ComboBox, EditableComboBox, FluentIcon,
    FluentWindow, InfoBadge, InfoBar, InfoLevel, LineEdit, NavigationItemPosition,
    PlainTextEdit, PrimaryPushButton, ProgressBar, PushButton, ScrollArea, SimpleCardWidget,
    SpinBox, StrongBodyLabel, Theme, TitleLabel, TransparentToolButton,
    isDarkTheme, qconfig, setTheme, setThemeColor,
)

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui import config_manager
from gui.backend_runner import BackendRunner
from gui.download_worker import ModelDownloadWorker
from gui.local_model_controller import LocalModelController
from gui.service_catalog_widget import ServiceCatalogWidget
from gui.wheel_guard import protect_wheel_controls
from gui.setup_assistant import (
    SETUP_ASSISTANT_VERSION,
    SetupAssistantDialog,
    apply_recommendation,
    mark_setup_assistant_seen,
    recommendations_for_hardware,
)
from backend.local_runtime import detect_hardware
from backend.model_manager import WHISPER_MODEL_URLS, model_filename

MODEL_OPTIONS = [
    ("tiny", "Tiny"),
    ("base", "Base"),
    ("small", "Small"),
    ("medium", "Medium"),
    ("large-v3", "Large v3"),
]
SUBTITLE_MAX_CHARS_DEFAULT = 260
SUBTITLE_MAX_SENTENCES_DEFAULT = 2
SUBTITLE_MAX_SENTENCES_CJK = 1
SUBTITLE_HISTORY_LINES_DEFAULT = 2
SUBTITLE_SHOW_PARTIAL_DEFAULT = True
STALL_TIMEOUT_DEFAULT = 15
STALL_CHECK_INTERVAL_DEFAULT = 5


def _fluent_palette(dark: bool) -> QtGui.QPalette:
    """Application palette matching the Fluent theme, for widgets Fluent does not style."""
    c = QtGui.QColor
    window, base, button, text, muted = (
        (c(32, 32, 32), c(43, 43, 43), c(50, 50, 50), c(255, 255, 255), c(160, 160, 160))
        if dark
        else (c(243, 243, 243), c(255, 255, 255), c(253, 253, 253), c(0, 0, 0), c(110, 110, 110))
    )
    accent = c(47, 118, 232)
    palette = QtGui.QPalette()
    for role, color in (
        (QtGui.QPalette.Window, window), (QtGui.QPalette.WindowText, text),
        (QtGui.QPalette.Base, base), (QtGui.QPalette.AlternateBase, button),
        (QtGui.QPalette.Text, text), (QtGui.QPalette.Button, button),
        (QtGui.QPalette.ButtonText, text), (QtGui.QPalette.ToolTipBase, base),
        (QtGui.QPalette.ToolTipText, text), (QtGui.QPalette.PlaceholderText, muted),
        (QtGui.QPalette.Highlight, accent), (QtGui.QPalette.HighlightedText, c(255, 255, 255)),
        (QtGui.QPalette.Link, accent),
    ):
        palette.setColor(role, color)
    for role in (QtGui.QPalette.Text, QtGui.QPalette.WindowText, QtGui.QPalette.ButtonText):
        palette.setColor(QtGui.QPalette.Disabled, role, muted)
    return palette


class _PageIndexView:
    """Legacy page-index view (the old QTabWidget API) over the Fluent page stack."""

    def __init__(self, window):
        self.window = window

    def count(self):
        return len(self.window.pages)

    def tabText(self, index):
        return self.window.page_names[index]

    def widget(self, index):
        return self.window.pages[index]

    def currentIndex(self):
        return self.window._current_page

    def setCurrentIndex(self, index):
        self.window.switchTo(self.window.pages[index])


class MainWindow(FluentWindow):
    """Desktop control surface with a persistent sidebar and stacked pages.

    ``auto_start`` and ``setup_tray`` are explicit seams for offscreen render
    tests. Production defaults retain the existing tray and automatic startup
    behavior, while tests can create an isolated window without spawning a
    backend or touching the user's config.
    """

    NAV_ITEMS = (
        ("Server", "server"),
        ("Local translation", "local"),
        ("STT models", "stt"),
        ("Tuning", "tuning"),
        ("Translation services", "services"),
        ("Settings", "settings"),
    )
    NAV_EXPAND_WIDTH = 220
    NAV_TO_TAB = {
        "server": 0,
        "local": 1,
        "stt": 2,
        "tuning": 3,
        "services": 5,
        "settings": 4,
    }

    THEMES = {"light": Theme.LIGHT, "dark": Theme.DARK, "system": Theme.AUTO}

    def __init__(
        self,
        config_path: Path | str | None = None,
        *,
        auto_start: bool | None = None,
        setup_tray: bool | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Live Subtitle")
        self.setMinimumSize(900, 640)

        explicit_config = config_path is not None
        if explicit_config:
            self.config_path = Path(config_path)
            config_existed = self.config_path.exists()
        else:
            user_config_path = config_manager.get_user_config_path()
            config_existed = user_config_path.exists()
            self.config_path = config_manager.ensure_user_config()
        if self.config_path.exists():
            self.config: Dict[str, Any] = self._load_config_snapshot()
        else:
            self.config = config_manager.load_default_config()
            config_manager.write_config(self.config_path, self.config)
        self.config_revision: Any = getattr(self, "config_revision", None)

        self.backend_runner: Optional[BackendRunner] = None
        self.download_worker: Optional[ModelDownloadWorker] = None
        self.tray: Optional[QtWidgets.QSystemTrayIcon] = None
        self.pending_start = False
        self.restart_pending = False
        self.force_close = False
        self.backend_failed = False
        self.last_backend_error: Optional[str] = None
        self._service_test_socket: Optional[QtWebSockets.QWebSocket] = None
        self._service_test_timer: Optional[QtCore.QTimer] = None
        self._service_test_result_received = False
        self._loading_ui = False
        self._dirty = False
        self._dirty_pages: set[int] = set()
        self._current_page = 0
        gui_config = self.config.get("gui") if isinstance(self.config.get("gui"), dict) else {}
        try:
            setup_version = int(gui_config.get("setup_assistant_version", 0))
        except (TypeError, ValueError):
            setup_version = 0
        self._show_setup_assistant_on_start = (
            not explicit_config
            and not config_existed
            and setup_version < SETUP_ASSISTANT_VERSION
        )
        self._auto_start_enabled = (
            bool(auto_start)
            if auto_start is not None
            else os.getenv("STT_GUI_DISABLE_AUTOSTART", "").strip().lower()
            not in {"1", "true", "yes", "on"}
        )
        self._setup_tray_enabled = (
            bool(setup_tray)
            if setup_tray is not None
            else os.getenv("STT_GUI_DISABLE_TRAY", "").strip().lower()
            not in {"1", "true", "yes", "on"}
        )
        self._ui_font_family = self._ensure_ui_font()
        self.setFont(QtGui.QFont(self._ui_font_family, 10))
        # Plain Qt widgets did not inherit the window font (they fell back to "Sans Serif 9").
        QtWidgets.QApplication.setFont(QtGui.QFont(self._ui_font_family, 10))
        setThemeColor("#2f76e8")  # the app's original brand blue (Fluent defaults to teal)
        setTheme(self.THEMES[self._theme_mode()])
        if QtGui.QGuiApplication.platformName() == "offscreen":
            self.setMicaEffectEnabled(False)

        self._build_ui()
        protect_wheel_controls(self)
        self._load_config_into_ui(self.config)
        if self._setup_tray_enabled:
            self._setup_tray()
        self._update_ws_url()
        self._update_buttons()
        self._shared_config_timer = QtCore.QTimer(self)
        self._shared_config_timer.setInterval(2500)
        self._shared_config_timer.timeout.connect(self._sync_shared_config)
        self._shared_config_timer.start()

        if self._show_setup_assistant_on_start:
            QtCore.QTimer.singleShot(0, self._run_first_start_setup_assistant)
        elif self._auto_start_enabled:
            QtCore.QTimer.singleShot(0, self._auto_start_if_valid)

    @staticmethod
    def _ensure_ui_font() -> str:
        """Load the Windows UI font for offscreen/high-DPI rendering too."""

        if QtGui.QGuiApplication.platformName() == "offscreen":
            for path in (Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts").glob("sego*.ttf"):
                QtGui.QFontDatabase.addApplicationFont(str(path))
        candidates = (
            Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf",
            Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
        )
        for font_path in candidates:
            if font_path.exists():
                font_id = QtGui.QFontDatabase.addApplicationFont(str(font_path))
                if font_id >= 0:
                    families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
                    if families:
                        return str(families[0])
        return "Sans Serif"

    def _build_ui(self) -> None:
        server_page = self._build_server_tab()
        local_panel = QtWidgets.QWidget()
        self.local_models = LocalModelController(
            local_panel,
            self._model_storage_root_from_config(self.config)
            / config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME,
            lambda: self.local_models.collect_into_config(self.config) if hasattr(self, "local_models") else self.config,
            self._persist_local_config,
            self,
        )
        for key in ("model", "target", "context", "gpu_layers", "port"):
            control = self.local_models.controls[key]
            signal = control.currentTextChanged if hasattr(control, "currentTextChanged") else control.valueChanged
            signal.connect(self._mark_dirty)
        self.local_models.activated.connect(self._on_local_model_activated)
        self.local_models.deactivated.connect(self._on_local_model_deactivated)
        self.local_models.failed.connect(lambda message: self._show_notice(message, "error"))
        local_page = self._wrap_scroll_page(local_panel)
        stt_page = self._build_stt_tab()
        tuning_page = self._build_tuning_tab()
        self.service_catalog = ServiceCatalogWidget()
        self.service_catalog.changed.connect(self._service_catalog_changed)
        self.service_catalog.save_requested.connect(self._save_service)
        self.service_catalog.notice.connect(lambda message: self._show_notice(message, "info"))
        self.service_catalog.test_requested.connect(self._test_translation_profile)
        self.services_page = self._build_services_page()
        settings_page = self._build_settings_page()
        # Keys preserve the config/save contract; insertion order is navigation order.
        self.pages = {0: server_page, 1: local_page, 2: stt_page, 3: tuning_page,
                      5: self.services_page, 4: settings_page}
        self.page_names = {self.NAV_TO_TAB[key]: label for label, key in self.NAV_ITEMS}
        self.tabs = _PageIndexView(self)
        # The library default (322 px) is ~36% of the 900 px minimum window; set it before the items exist.
        self.navigationInterface.setExpandWidth(self.NAV_EXPAND_WIDTH)
        icons = (FluentIcon.CONNECT, FluentIcon.LANGUAGE, FluentIcon.MICROPHONE,
                 FluentIcon.SETTING, FluentIcon.CLOUD, FluentIcon.SETTING)
        for (label, key), icon in zip(self.NAV_ITEMS, icons):
            page = self.pages[self.NAV_TO_TAB[key]]
            page.setObjectName(key)
            page.widget().layout().insertWidget(0, TitleLabel(label))
            self.addSubInterface(page, icon, label, isTransparent=True,
                                 position=NavigationItemPosition.BOTTOM if key == "settings"
                                 else NavigationItemPosition.TOP)
        self.navigationInterface.setCollapsible(False)
        self.navigationInterface.setReturnButtonVisible(False)
        self.stackedWidget.currentChanged.connect(self._on_page_changed)

        # FluentWindow is a QWidget: put the shared footer below its page stack.
        self.widgetLayout.removeWidget(self.stackedWidget)
        content = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(content)
        # Bottom gap: on a real Win11 window the status text otherwise sits ~2 px from the rounded edge.
        layout.setContentsMargins(0, 0, 0, 6)
        layout.addWidget(self.stackedWidget, 1)
        footer = QtWidgets.QHBoxLayout()
        footer.setContentsMargins(24, 8, 24, 8)
        self.dirty_label = CaptionLabel("")
        self.dirty_label.setWordWrap(True)
        footer.addWidget(self.dirty_label)
        footer.addStretch(1)
        self.restart_notice = CaptionLabel("")
        self.restart_notice.setWordWrap(True)
        footer.addWidget(self.restart_notice, 1)
        self.reset_btn = PushButton("Reset")
        self.save_btn = PrimaryPushButton("Save page")
        footer.addWidget(self.reset_btn)
        footer.addWidget(self.save_btn)
        layout.addLayout(footer)
        self.status_bar = QtWidgets.QStatusBar()
        self.status_bar.setSizeGripEnabled(False)
        qconfig.themeChangedFinished.connect(self._update_status_palette)
        self._update_status_palette()
        layout.addWidget(self.status_bar)
        self.status_bar.showMessage("Ready to manage the backend")
        self.save_btn.clicked.connect(self._save_defaults)
        self.reset_btn.clicked.connect(self._reset_defaults)
        self.widgetLayout.addWidget(content)
        self._on_page_changed(0)

    def _build_server_tab(self) -> QtWidgets.QWidget:
        content = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(content)
        body.setContentsMargins(0, 4, 4, 20)
        body.setSpacing(14)

        connection = self._card("Server connection")
        form = QtWidgets.QFormLayout()
        connection.layout().addLayout(form)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self.host_input = LineEdit()
        self.host_input.setPlaceholderText("127.0.0.1")
        self.port_input = SpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setMaximumWidth(160)
        ws_container = QtWidgets.QWidget()
        ws_layout = QtWidgets.QHBoxLayout(ws_container)
        ws_layout.setContentsMargins(0, 0, 0, 0)
        self.ws_url_input = LineEdit()
        self.ws_url_input.setReadOnly(True)
        self.copy_ws_btn = PushButton("Copy address")
        self.copy_ws_btn.setIcon(FluentIcon.COPY)
        self.copy_ws_btn.setProperty("kind", "secondary")
        ws_layout.addWidget(self.ws_url_input, 1)
        ws_layout.addWidget(self.copy_ws_btn)
        form.addRow("Host", self.host_input)
        form.addRow("Port", self.port_input)
        form.addRow("WebSocket URL", ws_container)
        body.addWidget(connection)

        controls = self._card("Server controls")
        controls_layout = controls.layout()
        control_row = QtWidgets.QHBoxLayout()
        self.start_btn = PrimaryPushButton("Start backend")
        self.start_btn.setIcon(FluentIcon.PLAY_SOLID)
        self.stop_btn = PushButton("Stop")
        self.stop_btn.setIcon(FluentIcon.PAUSE_BOLD)
        self.stop_btn.setProperty("kind", "secondary")
        self.restart_btn = PushButton("Restart")
        self.restart_btn.setIcon(FluentIcon.SYNC)
        self.restart_btn.setProperty("kind", "secondary")
        control_row.addWidget(self.start_btn)
        control_row.addWidget(self.stop_btn)
        control_row.addWidget(self.restart_btn)
        control_row.addStretch(1)
        self.server_status = InfoBadge.info("Stopped")
        self.server_status.setObjectName("statusBadge")
        self.server_status.setProperty("status", "idle")
        control_row.addWidget(self.server_status)
        controls_layout.addLayout(control_row)
        self.server_status_detail = BodyLabel(
            "Start the local service and confirm it is ready before connecting."
        )
        self.server_status_detail.setObjectName("mutedText")
        self.server_status_detail.setWordWrap(True)
        controls_layout.addWidget(self.server_status_detail)

        error_row = QtWidgets.QHBoxLayout()
        self.error_toggle = PushButton()
        self.error_toggle.setText("Details and diagnostics")
        self.error_toggle.setCheckable(True)
        self.error_toggle.setChecked(False)
        self.logs_btn = PushButton("View logs")
        self.logs_btn.setProperty("kind", "secondary")
        error_row.addWidget(self.error_toggle)
        error_row.addStretch(1)
        error_row.addWidget(self.logs_btn)
        controls_layout.addLayout(error_row)
        self.error_details = PlainTextEdit()
        self.error_details.setReadOnly(True)
        self.error_details.setMaximumHeight(110)
        self.error_details.setPlaceholderText("Backend diagnostics will appear here after a failed start.")
        self.error_details.setVisible(False)
        controls_layout.addWidget(self.error_details)
        body.addWidget(controls)

        lower = QtWidgets.QHBoxLayout()
        about = self._card("About the backend")
        about_layout = about.layout()
        about_label = BodyLabel(
            "The local service exposes WebSocket speech recognition and HTTP translation. "
            "Model downloads and profile saves are safe to manage while the server is stopped."
        )
        about_label.setObjectName("mutedText")
        about_label.setWordWrap(True)
        about_layout.addWidget(about_label)
        tips = self._card("Quick tips")
        tips_layout = tips.layout()
        tips_label = BodyLabel(
            "• Keep the port available\n• Model loading happens when a capture selects it\n• Use View logs when a dependency fails\n• Save settings before restarting"
        )
        tips_label.setObjectName("mutedText")
        tips_label.setWordWrap(True)
        tips_layout.addWidget(tips_label)
        lower.addWidget(about, 1)
        lower.addWidget(tips, 1)
        body.addLayout(lower)
        body.addStretch(1)

        self.host_input.textChanged.connect(self._update_ws_url)
        self.host_input.textChanged.connect(self._mark_dirty)
        self.port_input.valueChanged.connect(self._update_ws_url)
        self.port_input.valueChanged.connect(self._mark_dirty)
        self.copy_ws_btn.clicked.connect(self._copy_ws_url)
        self.start_btn.clicked.connect(self._start_backend)
        self.stop_btn.clicked.connect(self._stop_backend)
        self.restart_btn.clicked.connect(self._restart_backend)
        self.error_toggle.toggled.connect(self.error_details.setVisible)
        self.error_toggle.toggled.connect(lambda _checked: self.error_toggle.setText("Details and diagnostics"))
        self.logs_btn.clicked.connect(self._show_logs_dialog)
        return self._wrap_scroll_page(content)

    def _build_stt_tab(self) -> QtWidgets.QWidget:
        content = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(content)
        body.setContentsMargins(0, 4, 4, 20)
        body.setSpacing(14)

        model_group = self._card("STT model library")
        form = QtWidgets.QFormLayout()
        model_group.layout().addLayout(form)

        self.model_select_combo = ComboBox()
        self.model_refresh_btn = PushButton("Refresh")
        self.model_download_btn = PushButton("Download")
        model_select_row = QtWidgets.QWidget()
        model_select_layout = QtWidgets.QHBoxLayout(model_select_row)
        model_select_layout.setContentsMargins(0, 0, 0, 0)
        model_select_layout.addWidget(self.model_select_combo, 1)
        model_select_layout.addWidget(self.model_refresh_btn)
        model_select_layout.addWidget(self.model_download_btn)

        self.download_progress = ProgressBar()
        self.download_progress.setValue(0)
        self.download_progress.hide()
        self.download_label = BodyLabel("")

        form.addRow("Default model", model_select_row)
        form.addRow("Download progress", self.download_progress)
        form.addRow("", self.download_label)
        self.stt_language_input = EditableComboBox()
        self.stt_language_input.addItem("Automatic", userData="auto")
        self.stt_language_input.addItem("English", userData="en")
        self.stt_language_input.addItem("日本語", userData="ja")
        self.stt_language_input.addItem("繁體中文", userData="zh")
        self.stt_language_input.setToolTip("Open language values remain valid; enter a provider-supported code when needed.")
        self.stt_partial_check = CheckBox("Show partial recognition updates")
        form.addRow("Default language", self.stt_language_input)
        form.addRow("Recognition", self.stt_partial_check)
        body.addWidget(model_group)

        state_card = self._card("Availability")
        state_layout = state_card.layout()
        self.model_state_label = BodyLabel(
            "Installed models are selectable immediately. Missing models stay visible so the server can start without downloading them."
        )
        self.model_state_label.setObjectName("mutedText")
        self.model_state_label.setWordWrap(True)
        state_layout.addWidget(self.model_state_label)
        body.addWidget(state_card)
        body.addStretch(1)

        self.model_refresh_btn.clicked.connect(self._refresh_model_list)
        self.model_download_btn.clicked.connect(self._download_selected_model)
        self.model_select_combo.currentIndexChanged.connect(self._update_model_state)
        self.model_select_combo.currentIndexChanged.connect(self._mark_dirty)
        self.stt_language_input.currentTextChanged.connect(self._mark_dirty)
        self.stt_partial_check.toggled.connect(self._mark_dirty)

        return self._wrap_scroll_page(content)

    def _build_tuning_tab(self) -> QtWidgets.QWidget:
        content = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(content)
        body.setContentsMargins(0, 4, 4, 20)
        body.setSpacing(14)

        cleanup_group = self._card("Subtitle cleanup")
        cleanup_form = QtWidgets.QFormLayout()
        cleanup_group.layout().addLayout(cleanup_form)
        self.subtitle_max_chars_input = SpinBox()
        self.subtitle_max_chars_input.setRange(60, 2000)
        self.subtitle_max_chars_input.setValue(SUBTITLE_MAX_CHARS_DEFAULT)
        self.subtitle_max_sentences_default_input = SpinBox()
        self.subtitle_max_sentences_default_input.setRange(1, 6)
        self.subtitle_max_sentences_default_input.setValue(SUBTITLE_MAX_SENTENCES_DEFAULT)
        self.subtitle_max_sentences_cjk_input = SpinBox()
        self.subtitle_max_sentences_cjk_input.setRange(1, 6)
        self.subtitle_max_sentences_cjk_input.setValue(SUBTITLE_MAX_SENTENCES_CJK)
        self.subtitle_history_lines_input = SpinBox()
        self.subtitle_history_lines_input.setRange(1, 4)
        self.subtitle_history_lines_input.setValue(SUBTITLE_HISTORY_LINES_DEFAULT)
        self.subtitle_show_partial_check = CheckBox("Show partial line")
        cleanup_form.addRow("Max chars", self.subtitle_max_chars_input)
        cleanup_form.addRow(
            "Max sentences (Latin)", self.subtitle_max_sentences_default_input
        )
        cleanup_form.addRow(
            "Max sentences (CJK)", self.subtitle_max_sentences_cjk_input
        )
        cleanup_form.addRow("History lines", self.subtitle_history_lines_input)
        cleanup_form.addRow("", self.subtitle_show_partial_check)
        cleanup_group.setObjectName("card")
        body.addWidget(cleanup_group)

        stability_group = self._card("STT stability")
        stability_form = QtWidgets.QFormLayout()
        stability_group.layout().addLayout(stability_form)
        self.stall_timeout_input = SpinBox()
        self.stall_timeout_input.setRange(0, 300)
        self.stall_timeout_input.setValue(STALL_TIMEOUT_DEFAULT)
        self.stall_check_interval_input = SpinBox()
        self.stall_check_interval_input.setRange(0, 300)
        self.stall_check_interval_input.setValue(STALL_CHECK_INTERVAL_DEFAULT)
        stability_form.addRow("Stall timeout (sec)", self.stall_timeout_input)
        stability_form.addRow(
            "Stall check interval (sec)", self.stall_check_interval_input
        )
        stability_group.setObjectName("card")
        body.addWidget(stability_group)
        tuning_note = BodyLabel("Subtitle cleanup applies to new capture sessions. Saving does not restart an active session.")
        tuning_note.setObjectName("mutedText")
        tuning_note.setWordWrap(True)
        body.addWidget(tuning_note)
        body.addStretch(1)

        for widget in (
            self.subtitle_max_chars_input,
            self.subtitle_max_sentences_default_input,
            self.subtitle_max_sentences_cjk_input,
            self.subtitle_history_lines_input,
            self.stall_timeout_input,
            self.stall_check_interval_input,
        ):
            widget.valueChanged.connect(self._mark_dirty)
        self.subtitle_show_partial_check.toggled.connect(self._mark_dirty)

        return self._wrap_scroll_page(content)

    @staticmethod
    def _card(title: str) -> QtWidgets.QFrame:
        card = SimpleCardWidget()
        card.setObjectName("card")
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        label = StrongBodyLabel(title)
        label.setObjectName("cardTitle")
        layout.addWidget(label)
        return card

    @staticmethod
    def _wrap_scroll_page(widget: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        widget.layout().setContentsMargins(24, 18, 24, 24)
        scroll.setWidget(widget)
        widget.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        return scroll

    def _build_services_page(self) -> QtWidgets.QWidget:
        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(0, 4, 4, 20)
        layout.setSpacing(14)
        layout.addWidget(self.service_catalog)
        notice = BodyLabel(
            "Profiles are saved through the shared settings revision. The selection applies to the next capture."
        )
        notice.setObjectName("mutedText")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        return self._wrap_scroll_page(content)

    def _build_settings_page(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(0, 4, 4, 20)
        layout.setSpacing(14)
        appearance = self._card("Appearance")
        appearance_layout = appearance.layout()
        appearance_layout.addWidget(BodyLabel("Choose how the desktop surface follows your display settings."))
        self.theme_combo = ComboBox()
        for mode in self.THEMES:
            self.theme_combo.addItem(mode.title(), userData=mode)
        self.theme_combo.currentIndexChanged.connect(
            lambda i: self._set_theme_mode(self.theme_combo.itemData(i)))
        appearance_layout.addWidget(self.theme_combo)
        self.theme_status = BodyLabel("System theme changes are applied when this window receives a palette update.")
        self.theme_status.setObjectName("mutedText")
        self.theme_status.setWordWrap(True)
        appearance_layout.addWidget(self.theme_status)
        layout.addWidget(appearance)

        behavior = self._card("Settings and lifecycle")
        behavior_layout = behavior.layout()
        settings_row = QtWidgets.QWidget()
        settings_row_layout = QtWidgets.QHBoxLayout(settings_row)
        settings_row_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_location = BodyLabel()
        self.settings_location.setObjectName("mutedText")
        self.settings_location.setWordWrap(True)
        settings_row_layout.addWidget(self.settings_location, 1)
        self.open_settings_folder_btn = TransparentToolButton()
        self.open_settings_folder_btn.setObjectName("openSettingsFolderButton")
        self.open_settings_folder_btn.setIcon(FluentIcon.FOLDER)
        self.open_settings_folder_btn.setIconSize(QtCore.QSize(18, 18))
        self.open_settings_folder_btn.setToolTip("Open settings folder")
        self.open_settings_folder_btn.setAccessibleName("Open settings folder")
        settings_row_layout.addWidget(self.open_settings_folder_btn, 0, QtCore.Qt.AlignTop)
        behavior_layout.addWidget(settings_row)
        self.restart_explainer = BodyLabel(
            "Host, port, model defaults and profile edits are written atomically. Host/port changes apply after Restart; subtitle and language defaults apply to the next capture."
        )
        self.restart_explainer.setObjectName("mutedText")
        self.restart_explainer.setWordWrap(True)
        behavior_layout.addWidget(self.restart_explainer)
        setup_row = QtWidgets.QHBoxLayout()
        setup_explainer = BodyLabel(
            "Detect NVIDIA VRAM again and choose a safe Whisper + local translation pair."
        )
        setup_explainer.setObjectName("mutedText")
        setup_explainer.setWordWrap(True)
        self.setup_assistant_btn = PushButton("Recommend model setup")
        self.setup_assistant_btn.setProperty("kind", "secondary")
        self.setup_assistant_btn.setAccessibleName("Recommend model setup")
        setup_row.addWidget(setup_explainer, 1)
        setup_row.addWidget(self.setup_assistant_btn)
        behavior_layout.addLayout(setup_row)
        layout.addWidget(behavior)

        storage = self._card("Model storage")
        storage_layout = storage.layout()
        storage_help = BodyLabel(
            "One location for downloaded model assets. Whisper weights use Speech models; "
            "the built-in local translation runtime uses Local Translation Models."
        )
        storage_help.setObjectName("mutedText")
        storage_help.setWordWrap(True)
        storage_layout.addWidget(storage_help)
        storage_row = QtWidgets.QWidget()
        storage_row_layout = QtWidgets.QHBoxLayout(storage_row)
        storage_row_layout.setContentsMargins(0, 0, 0, 0)
        self.model_cache_input = LineEdit()
        self.model_cache_input.setPlaceholderText(str(config_manager.get_default_model_storage_dir()))
        self.model_cache_input.setAccessibleName("Model storage directory")
        self.model_cache_btn = PushButton("Browse")
        storage_row_layout.addWidget(self.model_cache_input, 1)
        storage_row_layout.addWidget(self.model_cache_btn)
        storage_layout.addWidget(storage_row)
        self.model_storage_paths = BodyLabel()
        self.model_storage_paths.setObjectName("mutedText")
        self.model_storage_paths.setWordWrap(True)
        storage_layout.addWidget(self.model_storage_paths)
        layout.addWidget(storage)

        self.open_settings_folder_btn.clicked.connect(self._open_settings_folder)
        self.setup_assistant_btn.clicked.connect(self._run_setup_assistant)
        self.model_cache_btn.clicked.connect(self._browse_model_dir)
        self.model_cache_input.textChanged.connect(self._mark_dirty)
        self.model_cache_input.editingFinished.connect(self._model_storage_changed)

        about = self._card("About")
        about_layout = about.layout()
        about_text = BodyLabel(
            "Live Subtitle · desktop control app\n"
            "Turn browser audio into live captions with optional translation."
        )
        about_text.setObjectName("mutedText")
        about_layout.addWidget(about_text)
        layout.addWidget(about)
        layout.addStretch(1)
        return self._wrap_scroll_page(tab)

    def _navigate(self, index: int) -> None:
        if 0 <= index < len(self.NAV_ITEMS):
            self.switchTo(self.pages[self.NAV_TO_TAB[self.NAV_ITEMS[index][1]]])

    def _on_page_changed(self, _index: int) -> None:
        index = next(i for i, page in self.pages.items()
                     if page is self.stackedWidget.currentWidget())
        previous = self._current_page
        pending_page = next(iter(self._dirty_pages)) if len(self._dirty_pages) == 1 else None
        if index != previous and self._dirty and not self._resolve_unsaved_changes(pending_page):
            self.stackedWidget.blockSignals(True)
            self.switchTo(self.pages[previous])
            self.stackedWidget.blockSignals(False)
            self.navigationInterface.setCurrentItem(self.pages[previous].objectName())
            return
        self._current_page = index
        label = self.page_names[index]
        notices = {
            "Server": "Restart to apply host and port.",
            "Local translation": "Install or remove files immediately.",
            "STT models": "Defaults apply to the next capture.",
            "Tuning": "Applies to the next capture.",
            "Translation services": "Applies to the next capture.",
            "Settings": "Theme previews now; Save page to keep it.",
        }
        self.restart_notice.setText(notices.get(label, ""))
        self.save_btn.setVisible(index in self.PAGE_FIELDS)

    def _mark_dirty(self, *_args: object) -> None:
        if self._loading_ui:
            return
        page = self._current_page
        sender = self.sender()
        if isinstance(sender, QtWidgets.QWidget):
            page = next((i for i, widget in self.pages.items()
                         if widget.isAncestorOf(sender)), page)
        self._dirty_pages.add(page)
        self._update_dirty_indicator()

    def _update_dirty_indicator(self) -> None:
        self._dirty = bool(self._dirty_pages)
        names = ", ".join(self.page_names[i] for i in sorted(self._dirty_pages) if i in self.PAGE_FIELDS)
        self.dirty_label.setText(f"Unsaved: {names}" if names else "")
        self.dirty_label.setProperty("status", "warning" if names else "idle")
        self.dirty_label.style().unpolish(self.dirty_label)
        self.dirty_label.style().polish(self.dirty_label)

    def _service_catalog_changed(self) -> None:
        if self._loading_ui:
            return
        if self.service_catalog.has_unsaved_changes():
            self._dirty_pages.add(5)
        else:
            self._dirty_pages.discard(5)
        self._update_dirty_indicator()

    def _save_service(self) -> bool:
        updated = self.service_catalog.collect_into_config(self.config)
        if not self._save_config_to_disk(updated=updated, page=5):
            self.service_catalog.editor_status.setText("Unsaved · save failed")
            return False
        self._show_notice("Service saved · applies to the next capture")
        return True

    def _resolve_unsaved_changes(self, page: int | None = None) -> bool:
        if not self._dirty:
            return True
        names = self.page_names[page] if page is not None else ", ".join(
            self.page_names[i] for i in sorted(self._dirty_pages))
        answer = QtWidgets.QMessageBox.question(
            self, "Unsaved settings", f"Save changes to {names} before continuing?",
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        if answer == QtWidgets.QMessageBox.Save:
            return self._save_page(page) if page is not None else self._save_config_to_disk()
        if answer == QtWidgets.QMessageBox.Discard:
            self._load_config_into_ui(self.config, page=page)
            if page is not None:
                self._dirty_pages.discard(page)
                self._update_dirty_indicator()
            return True
        return False

    def _sync_shared_config(self) -> None:
        """Show extension/backend edits in an open GUI when its form is clean."""
        if self._dirty or self._loading_ui or self.download_worker is not None:
            return
        if getattr(self.local_models, "busy", False) or getattr(self.local_models, "worker", None):
            return
        try:
            from backend.config_store import load_snapshot
            config, revision = load_snapshot(self.config_path)
        except (OSError, ValueError, TypeError):
            return
        if revision == self.config_revision:
            return
        self.config_revision = revision
        self._load_config_into_ui(config)
        self.status_bar.showMessage("Shared settings updated from the backend or extension", 4500)

    def _show_notice(self, message: str, kind: str = "info") -> None:
        self.status_bar.showMessage(message, 6000)
        factory = {"error": InfoBar.error, "warning": InfoBar.warning}.get(kind, InfoBar.info)
        bar = factory("", message, parent=self, duration=6000)
        bar.setMaximumWidth(max(300, self.width() - 48))
        bar.contentLabel.setWordWrap(True)
        bar.adjustSize()
        if hasattr(self, "server_status_detail") and kind == "error":
            self.server_status_detail.setText(message)

    def _run_first_start_setup_assistant(self) -> None:
        applied = self._run_setup_assistant(first_run=True)
        # A new recommendation usually points to files that have not been
        # downloaded yet. Keep the backend stopped so the next screen can
        # explain that state instead of immediately opening a second prompt.
        if not applied and self._auto_start_enabled:
            QtCore.QTimer.singleShot(0, self._auto_start_if_valid)

    def _run_setup_assistant(
        self, _checked: bool = False, *, first_run: bool = False
    ) -> bool:
        if not self._resolve_unsaved_changes():
            return False
        hardware = detect_hardware()
        dialog = SetupAssistantDialog(
            hardware,
            recommendations_for_hardware(hardware),
            self,
        )
        accepted = dialog.exec() == QtWidgets.QDialog.Accepted
        if accepted:
            updated = apply_recommendation(
                self._collect_config_from_ui(), dialog.selected_recommendation()
            )
        else:
            if not first_run:
                return False
            updated = mark_setup_assistant_seen(self.config)

        self._load_config_into_ui(updated)
        if not self._save_config_to_disk():
            return False
        if accepted:
            stt_nav = next(
                index
                for index, (_label, key) in enumerate(self.NAV_ITEMS)
                if key == "stt"
            )
            self._navigate(stt_nav)
            self._show_notice(
                "Recommendation saved. Download the speech model here, then review the local translation download.",
                "info",
            )
        elif first_run:
            self._show_notice(
                "Model setup skipped. You can run it later from Settings.", "info"
            )
        return accepted

    def _test_translation_profile(self, profile: object) -> None:
        """Test the current service draft without publishing it."""

        if not isinstance(profile, dict):
            self._show_notice("Select a saved service before testing it.", "error")
            return
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id:
            self._show_notice("This service has no stable ID to test.", "error")
            return
        if self.backend_runner is None or not self.backend_runner.isRunning() or self.server_status.text() != "Ready":
            self.service_catalog.editor_status.setText("Start the backend and wait for Ready before testing a service.")
            return

        old_socket = self._service_test_socket
        if old_socket is not None:
            old_socket.blockSignals(True)
            old_socket.close()
            old_socket.deleteLater()
        socket = QtWebSockets.QWebSocket(
            "",
            QtWebSockets.QWebSocketProtocol.Version.Version13,
            self,
        )
        self._service_test_socket = socket
        self._service_test_result_received = False
        self.service_catalog.set_test_busy(True)
        self.service_catalog.editor_status.setText("Testing connection…")
        socket.connected.connect(
            lambda: socket.sendTextMessage(
                json.dumps(
                    {
                        "type": "test",
                        "target": "translation",
                        "version": 2,
                        "action": "translate",
                        "draft_profile": {
                            **{key: value for key, value in profile.items() if not key.startswith("_")},
                            "engine": {"api": "openai_compatible", "ollama": "ollama", "nllb": "nllb"}.get(
                                profile.get("kind"), profile.get("engine")
                            ),
                        },
                        "translation": {
                            "selection": {"kind": "profile", "id": profile_id}
                        },
                    }
                )
            )
        )
        socket.textMessageReceived.connect(self._on_service_test_message)
        socket.errorOccurred.connect(self._on_service_test_socket_error)
        socket.disconnected.connect(self._on_service_test_disconnected)
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(
            lambda: self._finish_service_test(False, "Service test timed out.")
        )
        self._service_test_timer = timer
        timer.start(90000)  # Allow the provider's first-request model load timeout.
        host = self.host_input.text().strip() or "127.0.0.1"
        if host == "0.0.0.0":
            host = "127.0.0.1"
        elif host in {"::", "[::]"}:
            host = "[::1]"
        elif ":" in host and not host.startswith("["):
            host = f"[{host}]"
        socket.open(QtCore.QUrl(f"ws://{host}:{self.port_input.value()}/asr"))
        self._show_notice(f"Testing {profile.get('name') or profile_id}…")

    def _on_service_test_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        message_type = payload.get("type")
        if message_type == "test_result":
            self._finish_service_test(
                bool(payload.get("ok")),
                str(payload.get("message") or "Service test completed."),
            )
        elif message_type == "error":
            self._finish_service_test(False, str(payload.get("message") or "Service test failed."))

    def _on_service_test_socket_error(self, _error: object = None) -> None:
        socket = self._service_test_socket
        if socket is None or self._service_test_result_received:
            return
        self._finish_service_test(False, socket.errorString() or "Could not connect to the backend.")

    def _on_service_test_disconnected(self) -> None:
        if not self._service_test_result_received:
            self._finish_service_test(False, "Backend closed the service test before returning a result.")

    def _finish_service_test(self, ok: bool, message: str) -> None:
        if self._service_test_result_received:
            return
        self._service_test_result_received = True
        timer = self._service_test_timer
        self._service_test_timer = None
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        socket = self._service_test_socket
        self._service_test_socket = None
        if socket is not None:
            socket.blockSignals(True)
            socket.close()
            socket.deleteLater()
        self.service_catalog.set_test_busy(False)
        self.service_catalog.editor_status.setText(("Test passed: " if ok else "Test failed: ") + message)

    def _show_logs_dialog(self) -> None:
        dialog = QtWidgets.QDialog(self)
        dialog.setPalette(self.status_bar.palette())
        dialog.setWindowTitle("Backend logs")
        dialog.resize(760, 420)
        layout = QtWidgets.QVBoxLayout(dialog)
        output = PlainTextEdit()
        output.setReadOnly(True)
        lines = getattr(self, "_log_lines", [])
        output.setPlainText("\n".join(lines) or "No backend output captured yet.")
        layout.addWidget(output)
        close_btn = PushButton("Close")
        close_btn.clicked.connect(dialog.reject)
        layout.addWidget(close_btn)
        dialog.exec()

    def _reapply_mica(self) -> None:
        # Qt resets the DWM backdrop when the application palette changes, so the Mica
        # light/dark choice that Fluent just applied is lost; re-assert it for the live theme.
        if self.isVisible() and self.isMicaEffectEnabled():
            self.windowEffect.setMicaEffect(self.winId(), isDarkTheme())

    def _update_status_palette(self) -> None:
        # Plain Qt widgets (labels, list views, QMessageBox) read the application palette,
        # which Fluent does not touch, so keep it in step with the Fluent theme. The flag
        # stops our own palette change from re-triggering the system-theme refresh in event().
        self._palette_syncing = True
        try:
            QtWidgets.QApplication.setPalette(_fluent_palette(isDarkTheme()))
        finally:
            self._palette_syncing = False
        self._reapply_mica()
        QtCore.QTimer.singleShot(0, self._reapply_mica)  # Qt may apply the palette change late
        palette = self.status_bar.palette()
        palette.setColor(QtGui.QPalette.Window, QtGui.QColor(32, 32, 32) if isDarkTheme()
                         else QtGui.QColor(240, 244, 249))
        palette.setColor(QtGui.QPalette.WindowText, QtCore.Qt.white if isDarkTheme() else QtCore.Qt.black)
        self.status_bar.setPalette(palette)

    def _theme_mode(self) -> str:
        if hasattr(self, "_theme_preference"):
            return self._theme_preference
        gui = self.config.get("gui") if isinstance(self.config, dict) else {}
        gui_map = gui if isinstance(gui, dict) else {}
        value = str(gui_map.get("theme") or "system").strip().lower()
        return value if value in {"light", "dark", "system"} else "system"

    def _set_theme_mode(self, mode: str, *, persist: bool = True) -> None:
        if mode not in self.THEMES:
            return
        self._theme_preference = mode
        if mode == "system":
            qconfig.theme = Theme.AUTO
        setTheme(self.THEMES[mode])
        if hasattr(self, "theme_combo"):
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentIndex(self.theme_combo.findData(mode))
            self.theme_combo.blockSignals(False)
        if hasattr(self, "theme_status"):
            self.theme_status.setText(
                f"Theme preference: {mode.title()}. System follows the current OS palette."
            )
        if persist and not self._loading_ui:
            self._save_ui_preference()

    def _save_ui_preference(self) -> None:
        self._dirty_pages.add(4)
        self._update_dirty_indicator()

    def _setup_tray(self) -> None:
        icon = self.style().standardIcon(QtWidgets.QStyle.SP_MediaPlay)
        self.tray = QtWidgets.QSystemTrayIcon(icon, self)
        menu = QtWidgets.QMenu()

        open_action = menu.addAction("Open")
        start_action = menu.addAction("Start")
        stop_action = menu.addAction("Stop")
        restart_action = menu.addAction("Restart")
        menu.addSeparator()
        quit_action = menu.addAction("Quit")

        open_action.triggered.connect(self.showNormal)
        start_action.triggered.connect(self._start_backend)
        stop_action.triggered.connect(self._stop_backend)
        restart_action.triggered.connect(self._restart_backend)
        quit_action.triggered.connect(self._quit_from_tray)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason == QtWidgets.QSystemTrayIcon.Trigger:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _quit_from_tray(self) -> None:
        self.force_close = True
        self.close()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if not self._resolve_unsaved_changes():
            event.ignore()
            return
        if self._service_test_socket is not None:
            self._finish_service_test(False, "Window closing.")
        if not self.local_models.shutdown():
            self.status_bar.showMessage("Waiting for model operation to stop...")
            event.ignore()
            QtCore.QTimer.singleShot(500, self.close)
            return
        self._stop_backend()
        if self.backend_runner and not self.backend_runner.wait(6000):
            event.ignore()
            QtCore.QTimer.singleShot(500, self.close)
            return
        event.accept()
        QtWidgets.QApplication.quit()

    def event(self, event: QtCore.QEvent) -> bool:
        # QWidget does not forward ApplicationPaletteChange to changeEvent.
        if (
            event.type() == QtCore.QEvent.ApplicationPaletteChange
            and hasattr(self, "config")
            and not getattr(self, "_palette_syncing", False)
            and self._theme_mode() == "system"
        ):
            # setTheme(AUTO) alone caches the previous OS theme when the mode is unchanged.
            qconfig.theme = Theme.AUTO
            setTheme(Theme.AUTO)
        return super().event(event)

    def changeEvent(self, event: QtCore.QEvent) -> None:
        if (
            event.type() == QtCore.QEvent.WindowStateChange
            and self.isMinimized()
            and getattr(self, "tray", None)
            and self.tray.isVisible()
        ):
            QtCore.QTimer.singleShot(0, self.hide)
            self.tray.showMessage(
                "Live Subtitle",
                "Minimized to tray",
                QtWidgets.QSystemTrayIcon.Information,
                1500,
            )
        super().changeEvent(event)

    def _update_ws_url(self) -> None:
        host = self.host_input.text().strip() or "127.0.0.1"
        port = self.port_input.value()
        self.ws_url_input.setText(f"ws://{host}:{port}/asr")

    def _copy_ws_url(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self.ws_url_input.text())
        self.status_bar.showMessage("WebSocket URL copied", 2000)

    def _update_buttons(self) -> None:
        running = self.backend_runner is not None and self.backend_runner.isRunning()
        stopping = getattr(self, "server_status", None) is not None and self.server_status.text() == "Stopping"
        downloading = self.download_worker is not None and self.download_worker.isRunning()
        self.start_btn.setEnabled(not running and not downloading)
        self.restart_btn.setEnabled(not downloading)
        self.stop_btn.setEnabled(running and not stopping)
        if hasattr(self, "model_download_btn"):
            self.model_download_btn.setEnabled(not downloading)

    def _set_status(self, text: str, status_type: str = "idle") -> None:
        if self.backend_failed and status_type != "error":
            return
        normalized = str(text).strip().lower()
        if status_type == "idle" and normalized in {"starting", "ready", "running"}:
            status_type = "running"
        elif status_type == "idle" and normalized in {"error", "failed", "stopped unexpectedly"}:
            status_type = "error"
        self.server_status.setText(text)
        self.server_status.setProperty("status", status_type)
        self.server_status.setLevel({"running": InfoLevel.SUCCESS, "error": InfoLevel.ERROR}.get(
            status_type, InfoLevel.INFOAMTION))
        if hasattr(self, "server_status_detail"):
            if text in {"Starting", "Ready", "Stopping", "Stopped"}:
                detail = {
                    "Starting": "Starting the local service…",
                    "Ready": "The local service is ready for connections.",
                    "Stopping": "Stopping the local service…",
                    "Stopped": "The service is stopped. Downloads and settings remain available.",
                }.get(text, text)
                self.server_status_detail.setText(detail)
        self.status_bar.showMessage(text)
        if self.tray:
            self.tray.setToolTip(text)

    def _auto_start_if_valid(self) -> None:
        if not self._validate_server_settings(show_error=False):
            self._set_status("Invalid config", "error")
            return
        self._start_backend()

    def _validate_server_settings(self, show_error: bool = True) -> bool:
        host = self.host_input.text().strip()
        port = self.port_input.value()
        if not host:
            self.host_input.setText("127.0.0.1")
            self._update_ws_url()
        if port < 1 or port > 65535:
            if show_error:
                self._set_status("Port must be 1-65535", "error")
            return False
        return True

    def _resolve_model_target(self) -> tuple[Path, str]:
        model_name = self.model_select_combo.currentData() or "medium"
        cache_dir = self._get_model_dir()
        return cache_dir / model_filename(model_name), model_name

    def _start_download(self, target_path: Path, model_name: str) -> None:
        url = WHISPER_MODEL_URLS.get(model_name)
        if not url:
            self._set_status("Unknown model size", "error")
            return
        self.download_worker = ModelDownloadWorker(url, target_path)
        self.download_worker.progress.connect(self._on_download_progress)
        self.download_worker.finished.connect(self._on_download_finished)
        self.download_progress.show()
        self.download_progress.setValue(0)
        self.download_label.setText(f"Downloading {model_name}...")
        self.download_worker.start()
        self._update_buttons()

    def _on_download_progress(self, downloaded: int, total: int) -> None:
        if total > 0:
            percent = int(downloaded / total * 100)
            self.download_progress.setValue(percent)
            self.download_label.setText(f"{percent}% ({downloaded // (1024*1024)} MB / {total // (1024*1024)} MB)")
        else:
            self.download_progress.setRange(0, 0)
            self.download_label.setText(f"{downloaded // (1024*1024)} MB")

    def _on_download_finished(self, ok: bool, message: str) -> None:
        if self.download_worker:
            self.download_worker.deleteLater()
        self.download_worker = None
        self.download_progress.setRange(0, 100)
        if ok:
            self.download_progress.setValue(100)
            self.download_label.setText("Download complete")
            if self.pending_start:
                self.pending_start = False
                self._start_backend()
        else:
            self.download_label.setText(message or "Download failed")
            self._set_status("Download failed", "error")
            self.pending_start = False
        self._update_buttons()

    def _ensure_model_ready(self) -> bool:
        target_path, model_name = self._resolve_model_target()
        is_standard = model_name in WHISPER_MODEL_URLS
        if target_path.exists():
            return True
        if not is_standard:
            QtWidgets.QMessageBox.warning(
                self,
                "Model missing",
                "Selected model file was not found in the directory.",
            )
            self._set_status("Model missing", "error")
            return False
        reply = QtWidgets.QMessageBox.question(
            self,
            "Model missing",
            f"{model_name}.pt was not found. Download it now?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            self._set_status("Model missing", "error")
            return False
        self.pending_start = True
        self._start_download(target_path, model_name)
        return False

    def _on_runner_status(self, text: str) -> None:
        status_type = {
            "Starting": "running",
            "Ready": "running",
            "Stopping": "idle",
            "Stopped": "idle",
        }.get(text, "idle")
        self._set_status(text, status_type)

    def _on_runner_log(self, line: str) -> None:
        self._log_lines = (getattr(self, "_log_lines", []) + [str(line)])[-200:]
        if hasattr(self, "error_details"):
            self.error_details.setPlainText("\n".join(self._log_lines[-20:]))

    def _start_backend(self) -> None:
        self.backend_failed = False
        self.last_backend_error = None
        try:
            if self.local_models.busy:
                self._set_status("Preparing local translation", "idle")
                return
            if self.backend_runner and self.backend_runner.isRunning():
                self._set_status("Already running", "running")
                return
            if not self._validate_server_settings():
                return
            if not self._resolve_unsaved_changes():
                return
            host = self.host_input.text().strip()
            port = self.port_input.value()
            self.backend_runner = BackendRunner(str(self.config_path), host, port)
            self._log_lines = []
            self.backend_runner.status.connect(self._on_runner_status)
            self.backend_runner.log.connect(self._on_runner_log)
            self.backend_runner.error.connect(self._on_backend_error)
            self.backend_runner.started.connect(lambda: self._set_status("Ready", "running"))
            self.backend_runner.stopped.connect(self._on_backend_stopped)
            self.backend_runner.start()
            self._set_status("Starting", "running")
            self._update_buttons()
        except Exception as exc:
            self._on_backend_error(f"{type(exc).__name__}: {exc}")

    def _stop_backend(self) -> None:
        self.backend_failed = False
        self.last_backend_error = None
        if self.backend_runner and self.backend_runner.isRunning():
            self.backend_runner.stop()
            self._set_status("Stopping", "idle")
        self._update_buttons()

    def _restart_backend(self) -> None:
        if self.backend_runner and self.backend_runner.isRunning():
            self.restart_pending = True
            self._stop_backend()
            return
        self._start_backend()

    def _on_backend_stopped(self) -> None:
        if self.backend_runner:
            self.backend_runner.deleteLater()
        self.backend_runner = None
        if self.restart_pending:
            self.restart_pending = False
            self._start_backend()
            return
        if not self.backend_failed:
            self._set_status("Stopped", "idle")
        self._update_buttons()

    def _on_backend_error(self, message: str) -> None:
        self.backend_failed = True
        self.last_backend_error = message
        if hasattr(self, "error_details"):
            self.error_details.setPlainText("\n".join(getattr(self, "_log_lines", []) + [message]))
            self.error_details.setVisible(self.error_toggle.isChecked())
        self._set_status(message, "error")

    def _browse_model_dir(self) -> None:
        start_dir = self.model_cache_input.text().strip()
        if not start_dir:
            start_dir = str(config_manager.get_default_model_storage_dir())
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select model storage directory",
            start_dir,
        )
        if path:
            self.model_cache_input.setText(path)
            self._model_storage_changed()

    def _open_settings_folder(self) -> None:
        folder = self.config_path.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._show_notice(f"Could not create settings folder: {exc}", "error")
            return
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder))):
            self._show_notice(f"Could not open settings folder: {folder}", "error")

    def _model_storage_root_from_config(self, cfg: Dict[str, Any]) -> Path:
        storage = cfg.get("models") if isinstance(cfg.get("models"), dict) else {}
        configured = str(storage.get("root") or "").strip()
        if configured:
            return Path(configured).expanduser()

        stt = cfg.get("stt") if isinstance(cfg.get("stt"), dict) else {}
        stt_dir = str(stt.get("model_cache_dir") or "").strip()
        if stt_dir:
            path = Path(stt_dir).expanduser()
            if path.name.casefold() in {
                config_manager.SPEECH_MODELS_DIR_NAME.casefold(),
                "models",
            }:
                return path.parent

        local = cfg.get("local_llama") if isinstance(cfg.get("local_llama"), dict) else {}
        local_dir = str(local.get("root") or "").strip()
        if local_dir:
            path = Path(local_dir).expanduser()
            if path.name.casefold() in {
                config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME.casefold(),
                "managed-models",
            }:
                return path.parent

        # An explicitly supplied config path keeps tests and portable builds
        # self-contained; the production config lives in the default app dir.
        return self.config_path.parent

    def _get_model_storage_root(self) -> Path:
        configured = self.model_cache_input.text().strip()
        return Path(configured).expanduser() if configured else config_manager.get_default_model_storage_dir()

    def _get_model_dir(self) -> Path:
        return (
            self._get_model_storage_root()
            / config_manager.SPEECH_MODELS_DIR_NAME
        )

    def _get_local_model_root(self) -> Path:
        return (
            self._get_model_storage_root()
            / config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME
        )

    def _update_model_storage_paths(self) -> None:
        root = self._get_model_storage_root()
        self.model_storage_paths.setText(
            f"Speech models: {root / config_manager.SPEECH_MODELS_DIR_NAME}\n"
            f"Local translation models: "
            f"{root / config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME}"
        )

    def _model_storage_changed(self) -> None:
        self.local_models.set_root(self._get_local_model_root())
        self.local_models.refresh_state()
        self._update_model_storage_paths()
        self._refresh_model_list()
        self._mark_dirty()

    def _scan_model_dir(self, model_dir: Path) -> Dict[str, Path]:
        if not model_dir.exists():
            return {}
        return {path.stem: path for path in model_dir.glob("*.pt") if path.is_file()}

    def _download_selected_model(self) -> None:
        if self.download_worker and self.download_worker.isRunning():
            self._set_status("Download already running", "error")
            return
        target_path, model_name = self._resolve_model_target()
        if target_path.exists():
            self._set_status("Model already present", "idle")
            return
        if model_name not in WHISPER_MODEL_URLS:
            QtWidgets.QMessageBox.warning(
                self,
                "Model download unavailable",
                "Selected model is custom and has no download URL.",
            )
            return
        self._start_download(target_path, model_name)

    def _refresh_model_list(self, preferred: Optional[str] = None) -> None:
        model_dir = self._get_model_dir()
        available = self._scan_model_dir(model_dir)
        current = preferred or self.model_select_combo.currentData() or "medium"

        self.model_select_combo.blockSignals(True)
        self.model_select_combo.clear()

        for key, label in MODEL_OPTIONS:
            suffix = "" if key in available else " (download)"
            self.model_select_combo.addItem(f"{label}{suffix}", userData=key)

        for name in sorted(available.keys()):
            if name in WHISPER_MODEL_URLS:
                continue
            self.model_select_combo.addItem(f"{name} (custom)", userData=name)

        target_index = next(
            (i for i in range(self.model_select_combo.count()) if self.model_select_combo.itemData(i) == current),
            None,
        )
        if target_index is None:
            self.model_select_combo.addItem(f"{current} (missing)", userData=current)
            target_index = self.model_select_combo.count() - 1
        self.model_select_combo.setCurrentIndex(target_index)
        self.model_select_combo.blockSignals(False)
        self._update_model_state()

    def _update_model_state(self, *_args: object) -> None:
        """Show availability of the selected model independently of startup."""

        if not hasattr(self, "model_state_label") or not hasattr(self, "model_select_combo"):
            return
        model_name = str(self.model_select_combo.currentData() or "").strip()
        if not model_name:
            self.model_state_label.setText("Choose a model to see its availability.")
            return
        target = self._get_model_dir() / model_filename(model_name)
        if target.is_file():
            self.model_state_label.setText(
                f"{model_name} · Installed and selectable. Downloading is separate from starting the server."
            )
        else:
            self.model_state_label.setText(
                f"{model_name} · Missing from the model directory. The server can still start; download it before capture."
            )

    def _load_config_snapshot(self) -> Dict[str, Any]:
        """Load through the shared revision store when it is available."""

        try:
            from backend import config_store  # type: ignore
        except ImportError:
            config_store = None
        if config_store is not None and callable(getattr(config_store, "load_snapshot", None)):
            snapshot, revision = config_store.load_snapshot(self.config_path)
            self.config_revision = revision
            return dict(snapshot)
        self.config_revision = None
        return config_manager.load_config(self.config_path)

    def _save_config_to_disk(self, *, updated: Dict[str, Any] | None = None, page: int | None = None) -> bool:
        if updated is None:
            updated = self._collect_config_from_ui()
        try:
            try:
                from backend import config_store  # type: ignore
            except ImportError:
                config_store = None
            if config_store is not None and callable(getattr(config_store, "save_config", None)):
                self.config_revision = config_store.save_config(
                    self.config_path,
                    updated,
                    expected_revision=self.config_revision,
                )
            else:
                config_manager.write_config(self.config_path, updated)
        except Exception as exc:
            # Revision conflicts are actionable: retain the user's edits in
            # the controls and require a fresh reload instead of overwriting a
            # migration or extension write with a stale snapshot.
            self._show_notice(f"Could not save settings: {exc}", "error")
            return False
        self.config = updated
        if page is None or page == 5:
            self.service_catalog.mark_saved()
        if page is None:
            self._dirty_pages.clear()
        else:
            self._dirty_pages.discard(page)
        self._update_dirty_indicator()
        return True

    def _persist_local_config(self, cfg: Dict[str, Any]) -> None:
        try:
            try:
                from backend import config_store  # type: ignore
            except ImportError:
                config_store = None
            if config_store is not None and callable(getattr(config_store, "save_config", None)):
                self.config_revision = config_store.save_config(
                    self.config_path,
                    cfg,
                    expected_revision=self.config_revision,
                )
            else:
                config_manager.write_config(self.config_path, cfg)
        except Exception as exc:
            self._show_notice(f"Could not save local model settings: {exc}", "error")
            raise
        self.config = cfg

    def _on_local_model_activated(self, cfg: Dict[str, Any]) -> None:
        self.config = cfg
        if not self.service_catalog.has_unsaved_changes():
            self.service_catalog.load_from_config(cfg)
        self._dirty_pages.discard(1)
        self._update_dirty_indicator()
        self.status_bar.showMessage("Model available in the extension. Refresh desktop services.")

    def _on_local_model_deactivated(self, cfg: Dict[str, Any]) -> None:
        self.config = cfg
        if not self.service_catalog.has_unsaved_changes():
            self.service_catalog.load_from_config(cfg)
        self._dirty_pages.discard(1)
        self._update_dirty_indicator()
        self.status_bar.showMessage("Model choices updated; refresh desktop services in the extension.")

    # Only fields edited on a page participate in its save or reset.
    PAGE_FIELDS = {
        0: {"server": ("host", "port")},
        1: {"local_llama": ("context", "gpu_layers", "port", "model_id", "target_language"),
            "translation": ("target_language",)},
        2: {"stt": ("model", "language", "model_path"), "translation": ("partial",)},
        3: {"subtitle": ("max_chars", "max_sentences_default", "max_sentences_cjk", "history_lines", "show_partial"),
            "stt": ("stall_timeout_sec", "stall_check_interval_sec")},
        4: {"models": ("root",), "stt": ("model_cache_dir",), "local_llama": ("root",), "gui": ("theme",)},
    }

    def _save_page(self, page: int) -> bool:
        if page == 5:
            return self._save_service()
        collected = self._collect_config_from_ui()
        updated = copy.deepcopy(self.config)
        for section, keys in self.PAGE_FIELDS[page].items():
            target = updated.setdefault(section, {})
            source = collected.get(section, {})
            for key in keys:
                if key in source:
                    target[key] = copy.deepcopy(source[key])
                else:
                    target.pop(key, None)
        return self._save_config_to_disk(updated=updated, page=page)

    def _save_defaults(self) -> None:
        if self._save_page(self._current_page):
            self.status_bar.showMessage("Page saved · new capture defaults are ready", 3000)

    def _reset_defaults(self) -> None:
        page = self._current_page
        name = self.page_names[page]
        message = (f"Discard the {name} draft and restore saved services? Saved services will be kept."
                   if page == 5 else f"Reset {name} fields to defaults? Review them, then Save page to keep them.")
        answer = QtWidgets.QMessageBox.question(
            self, f"Reset {name}", message,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        if page == 5:
            self.service_catalog.discard_selected()
        else:
            defaults = config_manager.load_default_config()
            draft = self._collect_config_from_ui()
            for section, keys in self.PAGE_FIELDS[page].items():
                for key in keys:
                    if key in defaults.get(section, {}):
                        draft.setdefault(section, {})[key] = copy.deepcopy(defaults[section][key])
                    else:
                        draft.setdefault(section, {}).pop(key, None)
            self._load_config_into_ui(draft, page=page)
            self._dirty_pages.add(page)
            self._update_dirty_indicator()
        self.status_bar.showMessage(f"{name} reset", 2000)

    def _collect_config_from_ui(self) -> Dict[str, Any]:
        cfg = copy.deepcopy(self.config)
        cfg["server"] = dict(cfg.get("server") or {})
        cfg["stt"] = dict(cfg.get("stt") or {})
        cfg["subtitle"] = dict(cfg.get("subtitle") or {})
        cfg["models"] = dict(cfg.get("models") or {})

        cfg["server"]["host"] = self.host_input.text().strip() or "127.0.0.1"
        cfg["server"]["port"] = int(self.port_input.value())

        cfg["stt"].pop("simulstreaming", None)
        storage_root = self._get_model_storage_root()
        model_dir = storage_root / config_manager.SPEECH_MODELS_DIR_NAME
        cfg["models"]["root"] = str(storage_root)
        cfg["stt"]["model_cache_dir"] = str(model_dir)
        cfg["stt"].pop("model_path", None)
        cfg["stt"]["model"] = self.model_select_combo.currentData() or "medium"
        language_index = self.stt_language_input.currentIndex()
        cfg["stt"]["language"] = (
            self.stt_language_input.itemData(language_index)
            if language_index >= 0 and self.stt_language_input.currentText() == self.stt_language_input.itemText(language_index)
            else self.stt_language_input.currentText().strip() or "auto"
        )

        cfg["subtitle"]["max_chars"] = int(self.subtitle_max_chars_input.value())
        cfg["subtitle"]["max_sentences_default"] = int(
            self.subtitle_max_sentences_default_input.value()
        )
        cfg["subtitle"]["max_sentences_cjk"] = int(
            self.subtitle_max_sentences_cjk_input.value()
        )
        cfg["subtitle"]["history_lines"] = int(self.subtitle_history_lines_input.value())
        cfg["subtitle"]["show_partial"] = bool(
            self.subtitle_show_partial_check.isChecked()
        )
        cfg["stt"]["stall_timeout_sec"] = int(self.stall_timeout_input.value())
        cfg["stt"]["stall_check_interval_sec"] = int(
            self.stall_check_interval_input.value()
        )
        translation = cfg.setdefault("translation", {})
        translation["partial"] = bool(self.stt_partial_check.isChecked())
        cfg["gui"] = dict(cfg.get("gui") or {})
        cfg["gui"]["theme"] = self._theme_mode()
        cfg = self.service_catalog.collect_into_config(cfg)
        self.local_models.set_root(
            storage_root / config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME
        )
        cfg = self.local_models.collect_into_config(cfg)
        cfg.setdefault("local_llama", {})["root"] = str(
            storage_root / config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME
        )
        return cfg

    def _load_config_into_ui(self, cfg: Dict[str, Any], *, page: int | None = None) -> None:
        self._loading_ui = True
        if page is None:
            self.config = copy.deepcopy(cfg)
        self.config.setdefault("gui", {})
        self.settings_location.setText(
            f"Settings file\n{self.config_path}\nWrites use an atomic revision check when the shared backend store is available."
        )
        if page is None or page == 4:
            theme = str(cfg.get("gui", {}).get("theme") or "system")
            self._set_theme_mode(theme, persist=False)
        if page is None or page == 0:
            server_cfg = cfg.get("server", {})
            self.host_input.setText(str(server_cfg.get("host", "127.0.0.1")))
            try:
                self.port_input.setValue(int(server_cfg.get("port", 8765)))
            except (TypeError, ValueError):
                self.port_input.setValue(8765)

        if page is None or page in {2, 4}:
            stt_cfg = cfg.get("stt", {})
            model_path = stt_cfg.get("model_path")
            selected_model = stt_cfg.get("model", "medium")
            if model_path:
                try:
                    selected_model = Path(model_path).stem
                except OSError:
                    pass
            if page == 4:
                selected_model = self.model_select_combo.currentData()
        if page is None or page == 4:
            storage_root = self._model_storage_root_from_config(cfg)
            self.model_cache_input.setText(str(storage_root))
            self.local_models.set_root(
                storage_root / config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME
            )
            self._update_model_storage_paths()
        if page is None or page in {2, 4}:
            self._refresh_model_list(selected_model)
            model_index = next(
                (i for i in range(self.model_select_combo.count()) if self.model_select_combo.itemData(i) == selected_model),
                None,
            )
            if model_index is not None:
                self.model_select_combo.setCurrentIndex(model_index)

        if page is None or page == 2:
            language = stt_cfg.get("language", "auto")
            lang_index = self.stt_language_input.findData(language)
            if lang_index >= 0:
                self.stt_language_input.setCurrentIndex(lang_index)
            else:
                self.stt_language_input.setCurrentText(str(language))
            translation_cfg = cfg.get("translation", {})
            if not isinstance(translation_cfg, dict):
                translation_cfg = {}
            self.stt_partial_check.setChecked(bool(translation_cfg.get("partial", False)))

        if page is None or page == 3:
            stt_cfg = cfg.get("stt", {})
            subtitle_cfg = cfg.get("subtitle", {})
            try:
                self.subtitle_max_chars_input.setValue(
                    int(subtitle_cfg.get("max_chars", SUBTITLE_MAX_CHARS_DEFAULT))
                )
            except (TypeError, ValueError):
                self.subtitle_max_chars_input.setValue(SUBTITLE_MAX_CHARS_DEFAULT)
            try:
                self.subtitle_max_sentences_default_input.setValue(
                    int(
                        subtitle_cfg.get(
                            "max_sentences_default", SUBTITLE_MAX_SENTENCES_DEFAULT
                        )
                    )
                )
            except (TypeError, ValueError):
                self.subtitle_max_sentences_default_input.setValue(
                    SUBTITLE_MAX_SENTENCES_DEFAULT
                )
            try:
                self.subtitle_max_sentences_cjk_input.setValue(
                    int(
                        subtitle_cfg.get(
                            "max_sentences_cjk", SUBTITLE_MAX_SENTENCES_CJK
                        )
                    )
                )
            except (TypeError, ValueError):
                self.subtitle_max_sentences_cjk_input.setValue(
                    SUBTITLE_MAX_SENTENCES_CJK
                )
            try:
                self.subtitle_history_lines_input.setValue(
                    int(subtitle_cfg.get("history_lines", SUBTITLE_HISTORY_LINES_DEFAULT))
                )
            except (TypeError, ValueError):
                self.subtitle_history_lines_input.setValue(SUBTITLE_HISTORY_LINES_DEFAULT)
            self.subtitle_show_partial_check.setChecked(
                bool(subtitle_cfg.get("show_partial", SUBTITLE_SHOW_PARTIAL_DEFAULT))
            )

            try:
                self.stall_timeout_input.setValue(
                    int(stt_cfg.get("stall_timeout_sec", STALL_TIMEOUT_DEFAULT))
                )
            except (TypeError, ValueError):
                self.stall_timeout_input.setValue(STALL_TIMEOUT_DEFAULT)
            try:
                self.stall_check_interval_input.setValue(
                    int(
                        stt_cfg.get(
                            "stall_check_interval_sec", STALL_CHECK_INTERVAL_DEFAULT
                        )
                    )
                )
            except (TypeError, ValueError):
                self.stall_check_interval_input.setValue(STALL_CHECK_INTERVAL_DEFAULT)

        if page is None or page == 5:
            self.service_catalog.load_from_config(cfg)
        if page is None or page == 1:
            self.local_models.load_config(cfg)
        self._update_ws_url()
        self._loading_ui = False
        if page is None:
            self._dirty_pages.clear()
        self._update_dirty_indicator()


def main() -> None:
    # Qt 6 enables high-DPI scaling by default; preserve fractional desktop scaling.
    QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
