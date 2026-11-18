from __future__ import annotations

import copy
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6 import QtCore, QtGui, QtWebSockets, QtWidgets
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CheckBox, ComboBox, FluentIcon,
    FluentWindow, InfoBadge, InfoBar, InfoLevel, LineEdit, NavigationItemPosition,
    PlainTextEdit, PrimaryPushButton, ProgressBar, PushButton, ScrollArea, SimpleCardWidget,
    SpinBox, StrongBodyLabel, Theme, TitleLabel, TransparentToolButton,
    isDarkTheme, qconfig, setTheme, setThemeColor,
)

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui import config_manager, fluent_kit, update_check, update_dialog
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
STALL_TIMEOUT_DEFAULT = 15
# Same languages and order as the extension popup; a test compares the two lists.
LANGUAGE_OPTIONS = (
    ("en", "English"), ("ja", "Japanese"), ("zh-TW", "Chinese (Traditional)"),
    ("zh-CN", "Chinese (Simplified)"), ("ko", "Korean"), ("fr", "French"),
    ("de", "German"), ("es", "Spanish"), ("pt", "Portuguese"), ("it", "Italian"),
    ("ru", "Russian"), ("id", "Indonesian"), ("vi", "Vietnamese"), ("th", "Thai"),
)


def _configured_target_language(cfg: Dict[str, Any]) -> str:
    """The shared target language as the backend resolves it; the older local-model key is the fallback."""
    translation = cfg.get("translation") if isinstance(cfg.get("translation"), dict) else {}
    local = cfg.get("local_llama") if isinstance(cfg.get("local_llama"), dict) else {}
    return str(translation.get("target_language") or local.get("target_language") or "zh-TW")


def _one_line(text: str, limit: int = 140) -> str:
    """Status-bar text: the bar clips instead of wrapping, so cut long messages with an ellipsis."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


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


class LogsDialog(fluent_kit.FluentDialog):
    """Read-only view of the captured backend output."""

    def __init__(self, parent: QtWidgets.QWidget, text: str) -> None:
        super().__init__(parent)
        self.widget.setMinimumWidth(720)
        self.viewLayout.addWidget(StrongBodyLabel("Backend logs"))
        self.output = PlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlainText(text)
        self.output.setMinimumHeight(300)
        self.viewLayout.addWidget(self.output)
        self.set_buttons([("Close", True, self.accept)])


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

    ``auto_start``, ``setup_tray`` and ``check_updates`` are explicit seams for
    offscreen render tests. Production defaults retain the existing tray,
    automatic startup and start-up update check, while tests can create an
    isolated window without spawning a backend, touching the user's config or
    asking GitHub anything (an explicit ``config_path`` turns the update check
    off unless ``check_updates`` says otherwise).
    """

    NAV_ITEMS = (
        ("Server", "server"),
        ("Local translation", "local"),
        ("Speech recognition", "stt"),
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
        check_updates: bool | None = None,
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
        self.restart_pending = False
        self.force_close = False
        self.backend_failed = False
        self.last_backend_error: Optional[str] = None
        self._log_lines: list[str] = []
        self._service_test_socket: Optional[QtWebSockets.QWebSocket] = None
        self._service_test_timer: Optional[QtCore.QTimer] = None
        self._service_test_result_received = False
        self._loading_ui = False
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
        # Update check: the version comes from the extension manifest, the remembered answer lives in
        # update-check.json beside the settings file (not in config.yaml, so a check never bumps its revision).
        self._app_version = update_check.read_app_version()
        self._update_state_path = self.config_path.with_name("update-check.json")
        self._update_state = update_check.load_state(self._update_state_path)
        self._update_check_running = False
        self._update_bridge = update_dialog.UpdateBridge(self)
        self._update_bridge.done.connect(self._on_update_result)
        self._update_check_at_start = (
            bool(check_updates)
            if check_updates is not None
            else not explicit_config
            and os.getenv("STT_GUI_DISABLE_UPDATE_CHECK", "").strip().lower()
            not in {"1", "true", "yes", "on"}
        )
        self._ui_font_family = self._ensure_ui_font()
        self.setFont(QtGui.QFont(self._ui_font_family, 10))
        # Plain Qt widgets did not inherit the window font (they fell back to "Sans Serif 9").
        QtWidgets.QApplication.setFont(QtGui.QFont(self._ui_font_family, 10))
        setThemeColor("#2f76e8")  # the app's original brand blue (Fluent defaults to teal)
        setTheme(self.THEMES[self._theme_mode()])
        # Mica shows the wallpaper only while the window is active, so the look flipped whenever a
        # dialog took focus; a solid Fluent background is the same in every window state.
        self.setMicaEffectEnabled(False)
        # setTheme() above already started the background fade towards "transparent" (Mica was
        # still on); retarget it, or it lands after this and leaves the window see-through.
        self._updateBackgroundColor()

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
        if self._update_check_at_start and update_check.is_due(self._update_state):
            # Well after the window (and any first-run dialog) is up; offline, it ends quietly.
            QtCore.QTimer.singleShot(5000, self._start_update_check)

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
        for key in ("model", "context", "gpu_layers", "port"):
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
        self.service_catalog.changed.connect(self._services_page_changed)
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
        # Both notes share one left column. Side by side, each got ~150 px and the
        # "Unsaved: Server, Local translation, ..." list wrapped onto two lines.
        notes = QtWidgets.QVBoxLayout()
        notes.setSpacing(2)
        self.dirty_label = CaptionLabel("")
        self.dirty_label.setWordWrap(True)
        # An empty label would collapse and make the footer jump when a page becomes dirty.
        self.dirty_label.setMinimumHeight(self.dirty_label.fontMetrics().lineSpacing())
        notes.addWidget(self.dirty_label)
        self.restart_notice = CaptionLabel("")
        self.restart_notice.setWordWrap(True)
        notes.addWidget(self.restart_notice)
        footer.addLayout(notes, 1)
        self.reset_btn = PushButton("Reset page")
        self.save_btn = PrimaryPushButton("Save all")
        footer.addWidget(self.reset_btn)
        footer.addWidget(self.save_btn)
        layout.addLayout(footer)
        self.status_bar = QtWidgets.QStatusBar()
        self.status_bar.setFont(self.restart_notice.font())
        self.status_bar.setSizeGripEnabled(False)
        qconfig.themeChangedFinished.connect(self._update_status_palette)
        self._update_status_palette()
        status_row = QtWidgets.QHBoxLayout()
        # QStatusBar paints its message 6 px inside its left edge.
        status_row.setContentsMargins(18, 0, 24, 0)
        status_row.addWidget(self.status_bar)
        layout.addLayout(status_row)
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
        form.addRow(BodyLabel("Host"), self.host_input)
        form.addRow(BodyLabel("Port"), self.port_input)
        form.addRow(BodyLabel("WebSocket URL"), ws_container)
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
        self.error_details.setMaximumHeight(180)
        self.error_details.setPlaceholderText("Backend diagnostics will appear here after a failed start.")
        self.error_details.setVisible(False)
        controls_layout.addWidget(self.error_details)
        body.addWidget(controls)

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

        model_group = self._card("Speech recognition models")
        form = self.stt_model_form = QtWidgets.QFormLayout()
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
        self.download_label = BodyLabel("")

        form.addRow(BodyLabel("Default model"), model_select_row)
        form.addRow(BodyLabel("Download progress"), self.download_progress)
        form.addRow(self.download_label)
        form.setRowVisible(self.download_progress, False)
        form.setRowVisible(self.download_label, False)
        self.stt_language_input = fluent_kit.FreeTextComboBox()
        self.stt_language_input.addItem("Automatic", userData="auto")
        self.stt_language_input.addItem("English", userData="en")
        self.stt_language_input.addItem("Japanese", userData="ja")
        self.stt_language_input.addItem("Chinese", userData="zh")
        self.stt_language_input.setToolTip("Choose the spoken language of the audio, or enter a provider-supported code.")
        self.stt_partial_check = CheckBox("Translate partial recognition updates")
        form.addRow(BodyLabel("Spoken language"), self.stt_language_input)
        form.addRow(BodyLabel("Recognition"), self.stt_partial_check)
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

        body.addWidget(fluent_kit.muted_label(
            "Advanced settings. The defaults work for most videos; changes apply to the next capture.", small=True))
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
        fluent_kit.hint_row(
            cleanup_form, "Max chars", self.subtitle_max_chars_input,
            "Trim the start of lines over Max chars. "
            f"Example: 150 chars at 100 keeps the last 100. (Default {SUBTITLE_MAX_CHARS_DEFAULT})")
        fluent_kit.hint_row(
            cleanup_form, "Max sentences (Latin)", self.subtitle_max_sentences_default_input,
            "Over Max chars, keep the last N Latin sentences (e.g. English). "
            f"Example: 2 keeps two. (Default {SUBTITLE_MAX_SENTENCES_DEFAULT})")
        fluent_kit.hint_row(
            cleanup_form, "Max sentences (CJK)", self.subtitle_max_sentences_cjk_input,
            "Same rule for Chinese, Japanese and Korean, whose sentences are long. "
            f"Example: 1 keeps one. (Default {SUBTITLE_MAX_SENTENCES_CJK})")
        cleanup_group.setObjectName("card")
        body.addWidget(cleanup_group)

        stability_group = self._card("Speech recognition stability")
        stability_form = QtWidgets.QFormLayout()
        stability_group.layout().addLayout(stability_form)
        self.stall_timeout_input = SpinBox()
        self.stall_timeout_input.setRange(0, 300)
        self.stall_timeout_input.setValue(STALL_TIMEOUT_DEFAULT)
        fluent_kit.hint_row(
            stability_form, "Stall timeout (sec)", self.stall_timeout_input,
            "Restart speech recognition if audio arrives without text; 0 disables it. "
            f"Example: 15 = 15 seconds. (Default {STALL_TIMEOUT_DEFAULT})")
        stability_group.setObjectName("card")
        body.addWidget(stability_group)
        body.addStretch(1)

        for widget in (
            self.subtitle_max_chars_input,
            self.subtitle_max_sentences_default_input,
            self.subtitle_max_sentences_cjk_input,
            self.stall_timeout_input,
        ):
            widget.valueChanged.connect(self._mark_dirty)

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
        # A wrapped label still asks for the width of its longest unbreakable word (a model path, an
        # error's file name), which would push every card past the window edge. Wrap in the space given.
        for label in widget.findChildren(QtWidgets.QLabel):
            if label.wordWrap():
                label.setMinimumWidth(1)
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

        defaults = self._card("Defaults for new captures")
        defaults_form = QtWidgets.QFormLayout()
        defaults.layout().addLayout(defaults_form)
        self.target_language_input = fluent_kit.FreeTextComboBox()
        self.target_language_input.setMaxLength(128)
        self.target_language_input.setFixedWidth(260)
        self.target_language_input.setToolTip(
            "Enter any language name or code. Suggestions are optional; quality depends on the model.")
        for code, name in LANGUAGE_OPTIONS:
            self.target_language_input.addItem(name, userData=code)
        caption = BodyLabel("Target language")
        caption.setFixedWidth(180)
        defaults_form.addRow(caption, self.target_language_input)
        defaults_form.addRow(fluent_kit.muted_label(
            'Subtitles are translated into this language. The extension uses it when "Same as desktop" '
            "is chosen. Applies to the next capture.", small=True))
        self.target_language_input.currentTextChanged.connect(self._services_page_changed)
        layout.addWidget(defaults)
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
            "Save all keeps your edits on every page. Restart the backend after changing the host or port. Subtitle and language defaults apply to the next capture."
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
        self.version_label = BodyLabel(f"Version {self._app_version}" if self._app_version else "Version unknown")
        about_layout.addWidget(self.version_label)
        self.update_auto_check = CheckBox("Check for updates when the app starts")
        self.update_auto_check.setChecked(update_check.is_enabled(self._update_state))
        about_layout.addWidget(self.update_auto_check)
        update_row = QtWidgets.QHBoxLayout()
        self.update_check_btn = PushButton("Check for updates")
        self.update_check_btn.setAccessibleName("Check for updates")
        self.update_status = BodyLabel(update_check.describe_state(self._update_state, self._app_version))
        self.update_status.setObjectName("mutedText")
        self.update_status.setWordWrap(True)
        update_row.addWidget(self.update_check_btn)
        update_row.addWidget(self.update_status, 1)
        about_layout.addLayout(update_row)
        about_layout.addWidget(fluent_kit.muted_label(
            "The check asks GitHub for the latest release number, at most once a day. Nothing about you, "
            "your audio or your settings is sent, and nothing is downloaded or installed for you. "
            "This switch applies at once; it is not part of Save all.", small=True))
        self.update_check_btn.clicked.connect(self._check_updates_clicked)
        self.update_auto_check.toggled.connect(self._set_update_auto)
        layout.addWidget(about)
        layout.addStretch(1)
        return self._wrap_scroll_page(tab)

    def _navigate(self, index: int) -> None:
        if 0 <= index < len(self.NAV_ITEMS):
            self.switchTo(self.pages[self.NAV_TO_TAB[self.NAV_ITEMS[index][1]]])

    def _on_page_changed(self, _index: int) -> None:
        # Every page keeps its edits in live widgets, so moving between pages never needs a prompt;
        # unsaved edits are resolved once, by Save all or when leaving (close, start, setup).
        index = next(i for i, page in self.pages.items()
                     if page is self.stackedWidget.currentWidget())
        self._current_page = index
        label = self.page_names[index]
        notices = {
            "Server": "Restart to apply host and port.",
            "Local translation": "Install or remove files immediately.",
            "Speech recognition": "Defaults apply to the next capture.",
            "Tuning": "Applies to the next capture.",
            "Translation services": "Applies to the next capture.",
            "Settings": "Theme previews now; Save all to keep it.",
        }
        self.restart_notice.setText(notices.get(label, ""))

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

    @property
    def _dirty(self) -> bool:
        return bool(self._dirty_pages)

    def _update_dirty_indicator(self) -> None:
        names = ", ".join(self.page_names[i] for i in sorted(self._dirty_pages))
        self.dirty_label.setText(f"Unsaved: {names}" if names else "")
        self.dirty_label.setProperty("status", "warning" if names else "idle")
        self.dirty_label.style().unpolish(self.dirty_label)
        self.dirty_label.style().polish(self.dirty_label)

    def _target_language_changed(self) -> bool:
        return self.target_language_input.value("zh-TW") != _configured_target_language(self.config)

    def _services_page_changed(self, *_args: object) -> None:
        """Unsaved means a service draft or a target language that differs from the saved one."""
        if self._loading_ui:
            return
        if self.service_catalog.has_unsaved_changes() or self._target_language_changed():
            self._dirty_pages.add(5)
        else:
            self._dirty_pages.discard(5)
        self._update_dirty_indicator()

    def _save_service(self) -> bool:
        updated = self.service_catalog.collect_into_config(self.config)
        if not self._save_config_to_disk(updated=updated, services_only=True):
            self.service_catalog.editor_status.setText("Unsaved · save failed")
            return False
        self._show_notice("Service saved · applies to the next capture")
        return True

    def _resolve_unsaved_changes(self) -> bool:
        if not self._dirty:
            return True
        names = ", ".join(self.page_names[i] for i in sorted(self._dirty_pages))
        answer = fluent_kit.ask(
            self, "Unsaved settings", f"Save changes to {names} before continuing?",
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        if answer == QtWidgets.QMessageBox.Save:
            return self._save_config_to_disk()
        if answer == QtWidgets.QMessageBox.Discard:
            self._load_config_into_ui(self.config)
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
        self.status_bar.showMessage(_one_line(message), 6000)
        factory ={"error": InfoBar.error, "warning": InfoBar.warning}.get(kind, InfoBar.info)
        bar = factory("", message, parent=self, duration=6000)
        bar.setMaximumWidth(max(300, self.width() - 48))
        bar.contentLabel.setWordWrap(True)
        bar.adjustSize()
        if hasattr(self, "server_status_detail") and kind == "error":
            self.server_status_detail.setText(message)

    # --- update check (gui/update_check.py): find out and tell, never download or install anything ---

    def _check_updates_clicked(self, _checked: bool = False) -> None:
        self._start_update_check(manual=True)

    def _set_update_auto(self, enabled: bool) -> None:
        self._update_state["enabled"] = bool(enabled)
        if not update_check.save_state(self._update_state_path, self._update_state):
            self._show_notice("Could not save the update-check setting.", "warning")

    def _start_update_check(self, manual: bool = False) -> None:
        if self._update_check_running:
            return
        self._update_check_running = True
        self.update_check_btn.setEnabled(False)
        self.update_status.setText("Checking…")
        current, bridge = self._app_version, self._update_bridge

        def work() -> None:
            result = update_check.check(current)  # never raises: offline is just an error result
            try:
                bridge.done.emit(result, manual)
            except RuntimeError:  # the window was closed in the meantime
                pass

        threading.Thread(target=work, name="update-check", daemon=True).start()

    def _on_update_result(self, result: "update_check.Result", manual: bool) -> None:
        self._update_check_running = False
        self.update_check_btn.setEnabled(True)
        self._update_state = update_check.record_result(self._update_state, result)
        update_check.save_state(self._update_state_path, self._update_state)
        self.update_status.setText(update_check.describe_state(self._update_state, self._app_version))
        if result.status == "new":
            # Start-up checks stay quiet about a version the user skipped; the button always speaks.
            if manual:
                self._show_update_dialog(result.release)
            elif not update_check.is_skipped(self._update_state, result.release):
                self._show_update_bar(result.release)
        elif manual:
            if result.status == "current":
                self._show_notice(f"You are on the latest version ({self._app_version}).")
            else:
                self._show_notice(f"Could not check for updates: {result.error}", "warning")

    def _show_update_bar(self, release: "update_check.Release") -> None:
        bar = InfoBar.info(
            "Update available",
            f"Live Subtitle {release.version} is out (you have {self._app_version}).",
            parent=self, duration=-1)
        details = PushButton("What's new")
        details.clicked.connect(lambda: (bar.close(), self._show_update_dialog(release)))
        bar.addWidget(details)

    def _show_update_dialog(self, release: "update_check.Release") -> None:
        choice = update_dialog.show_update_dialog(self, release, self._app_version)
        if choice == "open":
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(release.url))
        elif choice == "skip":
            self._update_state["skipped"] = release.version
            update_check.save_state(self._update_state_path, self._update_state)
            self._show_notice(f"Version {release.version} skipped. Check for updates to see it again.")

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
                "Recommendation saved. Download the speech recognition model here, then review the local translation download.",
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
        lines = getattr(self, "_log_lines", [])
        LogsDialog(self, "\n".join(lines) or "No backend output captured yet.").exec()

    def _update_status_palette(self) -> None:
        # Plain Qt widgets (labels, list views) read the application palette,
        # which Fluent does not touch, so keep it in step with the Fluent theme. The flag
        # stops our own palette change from re-triggering the system-theme refresh in event().
        self._palette_syncing = True
        try:
            QtWidgets.QApplication.setPalette(_fluent_palette(isDarkTheme()))
        finally:
            self._palette_syncing = False
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

    def _set_status(self, text: str, status_type: str = "idle", detail: Optional[str] = None) -> None:
        """Badge and status bar take the short ``text``; ``detail`` is the longer reason shown wrapped."""
        if self.backend_failed and status_type != "error":
            return
        headline = detail or text
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
            detail = detail or {
                "Starting": "Starting the local service…",
                "Ready": "The local service is ready for connections.",
                "Stopping": "Stopping the local service…",
                "Stopped": "The service is stopped. Downloads and settings remain available.",
            }.get(text)
            if detail:
                self.server_status_detail.setText(detail)
        self.status_bar.showMessage(_one_line(headline))
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
        self.stt_model_form.setRowVisible(self.download_progress, True)
        self.stt_model_form.setRowVisible(self.download_label, True)
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
        self.stt_model_form.setRowVisible(self.download_progress, False)
        self.stt_model_form.setRowVisible(self.download_label, False)
        if ok:
            self._refresh_model_list()
            self._show_notice("Download complete")
        else:
            self._show_notice(message or "Download failed", "error")
            self._set_status("Download failed", "error")
        self._update_buttons()

    def _on_runner_status(self, text: str) -> None:
        status_type = {
            "Starting": "running",
            "Ready": "running",
            "Stopping": "idle",
            "Stopped": "idle",
        }.get(text, "idle")
        self._set_status(text, status_type)

    def _on_runner_log(self, line: str) -> None:
        self._log_lines = (self._log_lines + [str(line)])[-200:]
        self._refresh_diagnostics()

    def _refresh_diagnostics(self) -> None:
        """Failure reason first, then the log tail. The box wraps and scrolls, so nothing is clipped."""
        parts = [self.last_backend_error or "", "\n".join(self._log_lines[-20:])]
        self.error_details.setPlainText("\n\n".join(part for part in parts if part))

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
            if not self.restart_pending and not self._resolve_unsaved_changes():
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
            if not self._resolve_unsaved_changes():
                return
            self.restart_pending = True
            self._stop_backend()
            return
        self._start_backend()

    def _on_backend_stopped(self) -> None:
        if self.backend_runner:
            self.backend_runner.deleteLater()
        self.backend_runner = None
        if self.restart_pending:
            self._start_backend()
            self.restart_pending = False
            return
        if not self.backend_failed:
            self._set_status("Stopped", "idle")
        self._update_buttons()

    def _on_backend_error(self, message: str) -> None:
        self.backend_failed = True
        self.last_backend_error = message
        self._refresh_diagnostics()
        self.error_toggle.setChecked(True)  # open the box: the reason belongs where the failure shows
        self._set_status("Failed", "error", detail=message)

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
            fluent_kit.ask(
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

    def _save_config_to_disk(self, *, updated: Dict[str, Any] | None = None, services_only: bool = False) -> bool:
        if updated is None:
            updated = self._collect_config_from_ui()
        try:
            try:
                from backend import config_store  # type: ignore
            except ImportError:
                config_store = None
            if config_store is not None and callable(getattr(config_store, "save_config", None)):
                config_store.check_service_urls(updated, self.config)
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
        self.service_catalog.mark_saved()
        if services_only:
            # Saving one service leaves a pending target language unsaved.
            self._services_page_changed()
        else:
            self._dirty_pages.clear()
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
        1: {"local_llama": ("context", "gpu_layers", "port", "model_id")},
        2: {"stt": ("model", "language"), "translation": ("partial",)},
        3: {"subtitle": ("max_chars", "max_sentences_default", "max_sentences_cjk"),
            "stt": ("stall_timeout_sec",)},
        4: {"models": ("root",), "stt": ("model_cache_dir",), "local_llama": ("root",), "gui": ("theme",)},
    }

    def _save_defaults(self) -> None:
        """Save all: every page's edits (and the open service draft) are written in one revision."""
        if self._save_config_to_disk():
            self.status_bar.showMessage("Settings saved · new capture defaults are ready", 3000)

    def _reset_defaults(self) -> None:
        page = self._current_page
        name = self.page_names[page]
        message = (f"Discard unsaved changes on {name}? Saved services and the saved target language are kept."
                   if page == 5 else f"Reset {name} fields to defaults? Review them, then Save all to keep them.")
        answer = fluent_kit.ask(
            self, f"Reset {name}", message,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        if page == 5:
            self._loading_ui = True
            self.target_language_input.set_value(_configured_target_language(self.config))
            self._loading_ui = False
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
        cfg["stt"]["model"] = self.model_select_combo.currentData() or "medium"
        if (cfg["stt"]["model"] != self.config.get("stt", {}).get("model", "medium")
                or storage_root != self._model_storage_root_from_config(self.config)):
            cfg["stt"].pop("model_path", None)
        cfg["stt"]["language"] = self.stt_language_input.value("auto")

        cfg["subtitle"]["max_chars"] = int(self.subtitle_max_chars_input.value())
        cfg["subtitle"]["max_sentences_default"] = int(
            self.subtitle_max_sentences_default_input.value()
        )
        cfg["subtitle"]["max_sentences_cjk"] = int(
            self.subtitle_max_sentences_cjk_input.value()
        )
        cfg["stt"]["stall_timeout_sec"] = int(self.stall_timeout_input.value())
        translation = cfg.setdefault("translation", {})
        translation["partial"] = bool(self.stt_partial_check.isChecked())
        translation["target_language"] = self.target_language_input.value("zh-TW")
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
            f"Settings file\n{self.config_path}\nSave all saves your changes here."
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
            selected_model = stt_cfg.get("model", "medium")
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

        if page is None or page == 2:
            self.stt_language_input.set_value(str(stt_cfg.get("language", "auto")))
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
                self.stall_timeout_input.setValue(
                    int(stt_cfg.get("stall_timeout_sec", STALL_TIMEOUT_DEFAULT))
                )
            except (TypeError, ValueError):
                self.stall_timeout_input.setValue(STALL_TIMEOUT_DEFAULT)
        if page is None or page == 5:
            self.service_catalog.load_from_config(cfg)
            self.target_language_input.set_value(_configured_target_language(cfg))
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
